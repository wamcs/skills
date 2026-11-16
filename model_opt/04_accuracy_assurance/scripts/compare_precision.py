#!/usr/bin/env python3
"""精度回归对比模板脚本（04_accuracy_assurance · model_opt Phase 4）。

★ 本脚本是模板：AGENT 填写下方 5 个函数与 3 个常量，禁止重写度量与判定框架。
★ 防作弊设计：
  - 基线落盘：baseline 模式输出保存到磁盘，compare 模式加载后对比，不重新生成
  - 阈值事前锁定：阈值及来源（CLI / 方差校准 / 类型默认）写入报告，运行时不可放宽
  - 自然波动必测：baseline 模式 --runs ≥ 2 测 D_base；compare 按
    D_opt ≤ max(声明下限, 3 × P99(D_base)) 自动校准距离类阈值
  - 统一接受规则：shape 一致 + 无 NaN/Inf + 按输出类型的门禁指标；跨张量 worst-case 聚合

用法:
  # 1. 采集基线 + 自然波动（优化前，原始模型）
  python compare_precision.py --mode baseline --runs 3 --baseline-dir ./precision_baseline

  # 2. 对比（优化后；baseline 目录含 variance.json 时自动校准阈值）
  python compare_precision.py --mode compare --baseline-dir ./precision_baseline

度量按 SKILL.md「四」的五类输出选择：
  logits:     JS(softmax 后) + Top-1 一致率（门禁）＋ max_abs / P99 相对误差 / Top-k（辅助）
  embedding:  cosine（门禁）+ 范数相对误差
  dense:      MAE / RMSE + max_abs + P99 相对误差（门禁 rel_p99）
  generated:  同 dense（固定随机条件下比**最终输出**张量，如解码后图像；SSIM/LPIPS 经
              custom_metric 补充。多步迭代模型的中间态如去噪 latents 不得作为门禁比对对象
              ——迭代误差放大使其阈值变相成为 bit-exact，仅作诊断）
  structured: custom_metric 任务指标（IoU / RMSD 等）；缺省退化为 dense 度量

注意：baseline 含零元素时 rel_p99 会被放大，此时用 --max-abs 门禁（声明即生效）。
"""

import argparse
import json
import sys
from pathlib import Path

import torch

# ═══════════════════════════════════════════════════════════
# AGENT 填写区（填写以下内容，禁止修改下方框架）
# ═══════════════════════════════════════════════════════════

OUTPUT_TYPE = "logits"       # logits / embedding / dense / structured / generated（SKILL「四」五类）
SAMPLE_IDS = ["sample_1"]    # Phase 1 精度数据集样本 id；Level 1 子集 / Level 2 全量在此控制
TOPK = 5                     # logits Top-k 一致率的 k


def build_model():
    """构建模型并加载权重，返回 model（.eval() 由框架调用）。
    baseline 模式: 构建优化前的原始模型。
    compare 模式:  构建优化后的模型。
    """
    raise NotImplementedError("agent 填写：模型构建 + 权重加载")


def build_sample(sample_id: str):
    """构建确定性输入样本。同一 sample_id 必须返回完全相同的输入
    （与 Phase 1 基线采集时一致；禁止未设种子的随机数）。"""
    raise NotImplementedError("agent 填写：确定性输入构建")


def run_inference(model, sample):
    """执行一次前向推理，返回输出（Tensor / dict / list，仅含可比对的数值结构）。
    需过滤 padding 时在此切片有效位置；离散 token 序列可直接返回 int 张量
    （框架按完全匹配率比对，门禁 ≥ 0.995）。"""
    raise NotImplementedError("agent 填写：前向推理调用")


def output_type(sample_id: str):
    """该样本输出的五类之一；输出类型单一时缺省用 OUTPUT_TYPE。
    【依据】以真实推理接口和任务 head 为准，不凭模型骨干推断。"""
    return OUTPUT_TYPE


def custom_metric(a, b):
    """structured 输出的任务指标（IoU / RMSD / SSIM 等），或为任意类型补充门禁。
    a = 当前输出，b = baseline 输出。返回 {指标名: (数值, 是否通过)} 或 None。"""
    return None

# ═══════════════════════════════════════════════════════════
# 度量与判定框架（★禁止修改★）
# ═══════════════════════════════════════════════════════════

DEFAULT_THRESHOLDS = {
    "logits":     {"js": 1e-3, "top1": 0.995},
    "embedding":  {"cosine": 0.999},
    "dense":      {"rel_p99": 1e-2},
    "generated":  {"rel_p99": 1e-2},
    "structured": {},
}
GATES = {  # (指标, 方向)：le = 越小越好 / ge = 越大越好
    "logits":     [("js", "le"), ("top1", "ge")],
    "embedding":  [("cosine", "ge")],
    "dense":      [("rel_p99", "le")],
    "generated":  [("rel_p99", "le")],
    "structured": [],  # 门禁由 custom_metric 提供；缺省退化为 dense
}
DIST_METRICS = {"max_abs", "mae", "rmse", "rel_p99", "js", "norm_rel_err"}  # 聚合取 max，可方差校准
CONS_METRICS = {"cosine", "top1", "topk_acc", "match_acc"}                  # 聚合取 min，不校准


def _device():
    try:
        import torch_npu  # noqa: F401
        if torch.npu.is_available():
            return "npu"
    except ImportError:
        pass
    return "cuda" if torch.cuda.is_available() else "cpu"


def _collect(obj):
    """输出结构统一 detach 到 CPU（int 张量保持 int，用于离散匹配率）。"""
    if isinstance(obj, torch.Tensor):
        return obj.detach().cpu()
    if isinstance(obj, dict):
        return {k: _collect(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_collect(v) for v in obj]
    return obj


def _p99(t: torch.Tensor) -> float:
    """排序法 P99（torch.quantile 有元素数上限，不用）。"""
    t = t.flatten()
    if t.numel() == 0:
        return 0.0
    s, _ = torch.sort(t)
    return s[min(s.numel() - 1, int(0.99 * (s.numel() - 1)))].item()


def _tensor_metrics(a, b, otype):
    """单对 float 张量的全部度量。"""
    m = {}
    diff = (a - b).abs()
    m["max_abs"] = diff.max().item() if a.numel() else 0.0
    m["mae"] = diff.mean().item() if a.numel() else 0.0
    if a.numel() == 0:
        return m
    m["rmse"] = diff.pow(2).mean().sqrt().item()
    m["rel_p99"] = _p99(diff / b.abs().clamp_min(1e-12))
    af, bf = a.flatten(), b.flatten()
    cos = (af * bf).sum() / (af.norm() * bf.norm()).clamp_min(1e-12)
    m["cosine"] = cos.item()
    if otype == "logits" and a.dim() >= 1 and a.size(-1) > 1:
        p = torch.softmax(a, dim=-1).clamp_min(1e-12)
        q = torch.softmax(b, dim=-1).clamp_min(1e-12)
        mid = 0.5 * (p + q)
        js = 0.5 * ((p * (p / mid).log()).sum(-1) + (q * (q / mid).log()).sum(-1))
        m["js"] = js.mean().item()
        m["top1"] = (a.argmax(-1) == b.argmax(-1)).float().mean().item()
        k = min(TOPK, a.size(-1))
        ia = a.topk(k, dim=-1).indices.sort(dim=-1).values
        ib = b.topk(k, dim=-1).indices.sort(dim=-1).values
        m["topk_acc"] = (ia == ib).all(dim=-1).float().mean().item()
    if otype == "embedding":
        m["norm_rel_err"] = (a.norm() - b.norm()).abs().item() / max(b.norm().item(), 1e-12)
    return m


def _merge_metrics(dst, src):
    """跨张量聚合：距离类取 max、一致类取 min（worst-case 原则）。"""
    for k, v in src.items():
        if k not in dst:
            dst[k] = v
        elif k in DIST_METRICS:
            dst[k] = max(dst[k], v)
        else:
            dst[k] = min(dst[k], v)


def compare_structure(a, b, otype, metrics, fails, path="root"):
    """递归比较输出结构：聚合度量到 metrics，shape/NaN 失败写入 fails。"""
    if isinstance(a, torch.Tensor) and isinstance(b, torch.Tensor):
        if a.shape != b.shape:
            fails.append(f"{path}: shape {tuple(a.shape)} vs {tuple(b.shape)}")
            return
        if not torch.isfinite(a.float()).all() or not torch.isfinite(b.float()).all():
            fails.append(f"{path}: 含 NaN/Inf")
            return
        if a.is_floating_point():
            _merge_metrics(metrics, _tensor_metrics(a.float(), b.float(), otype))
        else:
            _merge_metrics(metrics, {"match_acc": (a == b).float().mean().item()})
        return
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            fails.append(f"{path}: keys {set(a)} vs {set(b)}")
            return
        for k in a:
            compare_structure(a[k], b[k], otype, metrics, fails, f"{path}.{k}")
        return
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            fails.append(f"{path}: len {len(a)} vs {len(b)}")
            return
        for i, (x, y) in enumerate(zip(a, b)):
            compare_structure(x, y, otype, metrics, fails, f"{path}[{i}]")
        return
    if a != b:
        fails.append(f"{path}: 值不等 ({a} vs {b})")


def _gate_list(otype, metrics, cli, has_custom=False):
    gates = list(GATES.get(otype, []))
    if "match_acc" in metrics:
        gates.append(("match_acc", "ge"))
    for name in cli:  # CLI 显式给出的指标一律成为门禁（含 max_abs 等辅助指标）
        if all(g[0] != name for g in gates):
            gates.append((name, "ge" if name in CONS_METRICS else "le"))
    if otype == "structured" and not gates and not has_custom:
        gates = GATES["dense"]  # 无 custom 门禁时才退化为 dense
    return gates


def _resolve_thresholds(cli, variance, otype, metrics, has_custom=False):
    """阈值优先级：CLI > 类型默认；距离类指标再被 3×P99(D_base) 抬高（校准公式）。"""
    out = {}
    for name, direction in _gate_list(otype, metrics, cli, has_custom):
        if name in cli:
            thr, src = cli[name], "cli"
        elif name in DEFAULT_THRESHOLDS.get(otype, {}):
            thr, src = DEFAULT_THRESHOLDS[otype][name], "default"
        elif name == "match_acc":
            thr, src = 0.995, "default"
        else:
            thr, src = None, None  # 仅记录不判定
        if thr is not None and variance and name in DIST_METRICS \
                and name in variance.get("p99", {}):
            cal = 3.0 * variance["p99"][name]
            if cal > thr:
                thr, src = cal, f"calibrated: 3×P99(D_base)={cal:.3e}"
        out[name] = (thr, src, direction)
    return out


def _write_json(obj, path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[report] → {p}")

# ═══════════════════════════════════════════════════════════
# 模式实现（★禁止修改★）
# ═══════════════════════════════════════════════════════════

def _run_baseline(args):
    """原始模型重复运行：第 1 次输出存为 baseline，runs ≥ 2 时测量自然波动 D_base。"""
    device = _device()
    model = build_model().to(device).eval()
    bdir = Path(args.baseline_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    pools = {}
    for sid in SAMPLE_IDS:
        otype = output_type(sid)
        sample = build_sample(sid)
        outs = []
        with torch.no_grad():
            for _ in range(args.runs):
                outs.append(_collect(run_inference(model, sample)))
        torch.save(outs[0], bdir / f"{sid}.pt")
        for i in range(len(outs)):
            for j in range(i + 1, len(outs)):
                metrics, fails = {}, []
                compare_structure(outs[i], outs[j], otype, metrics, fails)
                for k, v in metrics.items():
                    pools.setdefault(k, []).append(v)
        print(f"[baseline] {sid}: {args.runs} 次运行，输出已保存")
    if args.runs >= 2:
        variance = {"runs": args.runs,
                    "p99": {k: _p99(torch.tensor(v)) for k, v in pools.items()}}
        _write_json(variance, bdir / "variance.json")
        nondet = [k for k, v in pools.items()
                  if (k in DIST_METRICS and max(v) > 0) or (k == "match_acc" and min(v) < 1.0)]
        if nondet:
            print(f"[variance] baseline 非确定（自然波动指标: {sorted(nondet)}）——"
                  "compare 时距离类阈值将按 3×P99(D_base) 抬高")
        else:
            print("[variance] baseline 确定（重复运行 bit-exact）")
    print(f"[baseline] 完成 → {bdir}")


def _run_compare(args):
    """优化后模型与落盘 baseline 对比：按输出类型计算度量，校准阈值，输出报告。"""
    device = _device()
    model = build_model().to(device).eval()
    bdir = Path(args.baseline_dir)
    variance = None
    vpath = bdir / "variance.json"
    if vpath.exists() and not args.no_calibrate:
        variance = json.loads(vpath.read_text(encoding="utf-8"))
        print(f"[variance] 已加载 {vpath}，距离类阈值按 3×P99(D_base) 校准")
    cli = {k: v for k, v in {"js": args.js, "top1": args.top1, "cosine": args.cosine,
                             "rel_p99": args.rel_p99, "max_abs": args.max_abs}.items()
           if v is not None}
    report = {"mode": "compare", "samples": {}}
    overall = True
    for sid in SAMPLE_IDS:
        otype = output_type(sid)
        bpath = bdir / f"{sid}.pt"
        if not bpath.exists():
            raise SystemExit(f"缺少 baseline 输出: {bpath}（先运行 --mode baseline）")
        base = torch.load(bpath, map_location="cpu", weights_only=False)
        with torch.no_grad():
            out = _collect(run_inference(model, build_sample(sid)))
        metrics, fails = {}, []
        compare_structure(out, base, otype, metrics, fails)
        entry = {"output_type": otype, "metrics": metrics, "fails": fails}
        cm = custom_metric(out, base) or {}
        if cm:
            entry["custom"] = {k: {"value": v[0], "passed": v[1]} for k, v in cm.items()}
        gate_fail = []
        entry["thresholds"] = {}
        for name, (thr, src, direction) in _resolve_thresholds(
                cli, variance, otype, metrics, has_custom=bool(cm)).items():
            if thr is None or name not in metrics:
                continue
            entry["thresholds"][name] = {"value": thr, "source": src}
            val = metrics[name]
            ok = val <= thr if direction == "le" else val >= thr
            if not ok:
                gate_fail.append(f"{name}={val:.3e} 越限（{'≤' if direction == 'le' else '≥'} {thr:.3e}）")
        if any(not v["passed"] for v in entry.get("custom", {}).values()):
            gate_fail.append("custom_metric 未通过")
        if gate_fail:
            entry["gate_fail"] = gate_fail
        entry["passed"] = not fails and not gate_fail
        report["samples"][sid] = entry
        overall = overall and entry["passed"]
        status = "PASS" if entry["passed"] else "FAIL"
        detail = "; ".join(fails + gate_fail)
        print(f"[{status}] {sid} ({otype})" + (f": {detail}" if detail else ""))
    report["overall"] = bool(overall)
    _write_json(report, args.report)
    print(f"[{'PASS' if overall else 'FAIL'}] overall")
    sys.exit(0 if overall else 1)


def main():
    ap = argparse.ArgumentParser(description="精度回归对比模板（04_accuracy_assurance）")
    ap.add_argument("--mode", choices=["baseline", "compare"], required=True)
    ap.add_argument("--baseline-dir", default="./precision_baseline")
    ap.add_argument("--runs", type=int, default=3,
                    help="baseline 模式重复运行次数（≥2 测自然波动 D_base）")
    ap.add_argument("--report", default="precision_report.json")
    ap.add_argument("--js", type=float, default=None, help="JS 阈值（显式给出即成为门禁）")
    ap.add_argument("--top1", type=float, default=None)
    ap.add_argument("--cosine", type=float, default=None)
    ap.add_argument("--rel-p99", type=float, default=None, dest="rel_p99")
    ap.add_argument("--max-abs", type=float, default=None, dest="max_abs")
    ap.add_argument("--no-calibrate", action="store_true",
                    help="忽略 variance.json，不按自然波动校准")
    args = ap.parse_args()
    if args.mode == "baseline":
        _run_baseline(args)
    else:
        _run_compare(args)


if __name__ == "__main__":
    main()
