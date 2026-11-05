#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""优化总结报告生成脚本 (06_evidence_db)。

在整个优化确认结束(确认节点 C 用户确认停止)时执行, 汇总 evidence_db 一次生成两个产物。
**一体两面, 信息对齐**: summary.md 供人读, summary.json 为机器可读完整镜像,
两者由同一数据结构 (build_summary) 渲染, 不存在口径分叉:
  - summary.md    人读报告: 模型与环境 + 优化总览(总加速比/总收益) + 各轮一览
                  (轮内增量 + 累计 vs 基线双加速比) + 优化点明细(单独 A/B 数值/贡献率)
                  + 四节成本汇总 + 多卡切分(如有) + 数据说明
  - summary.json  机器可读完整镜像: metadata / 各轮(end_to_end/双加速比/轮级成本/优化点) +
                  aggregate(总加速比/总收益/计数/成本合计) + splitting + notes

只呈现事实性数据; 贡献率 = 该点收益/整个优化的总收益(基线 - 末轮 after), 仅统计
accepted 的优化点; 累计加速比锚定 metadata baselines.performance_ms; 统计分析由消费端自行进行。
成本/总收益等加和由脚本计算, 记录值与计算值不一致处写入「数据说明」(notes)。

用法:
  python3 generate_summary.py <evidence_db 目录> [--output summary.md]
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

import yaml

ROUND_RE = re.compile(r"^round_(\d+)\.yaml$")
ID_RE = re.compile(r"^(.+)-round_(\d+)-(\d{8})$")
COST_SECTIONS = ("analysis", "optimization", "debugging", "system")
COST_KEYS = ("input_cost", "output_cost", "time_hours")
COST_LABELS = {"analysis": "根因分析", "optimization": "方案落实", "debugging": "额外调试",
               "system": "系统"}
META_FIELDS = (("model", ("name", "domain", "architecture", "params")),
               ("test_data", ("characteristics", "input_spec")),
               ("baselines", ("performance", "performance_ms", "accuracy")),
               ("environment", ("hardware", "software_stack")))
META_LABELS = {
    ("model", "name"): "模型名称", ("model", "domain"): "领域",
    ("model", "architecture"): "架构", ("model", "params"): "参数规模",
    ("test_data", "characteristics"): "测试数据特征",
    ("test_data", "input_spec"): "输入规格",
    ("baselines", "performance"): "性能基线获取",
    ("baselines", "performance_ms"): "性能基线数值 (ms)",
    ("baselines", "accuracy"): "精度基线获取",
    ("environment", "hardware"): "硬件", ("environment", "software_stack"): "软件栈",
}


def is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def fmt(value, suffix=""):
    if value is None:
        return "—"
    if isinstance(value, float):
        value = round(value, 4)
        if value == int(value):
            value = int(value)
    return "%s%s" % (value, suffix)


def load_yaml(path):
    """返回 (data, None) 或 (None, 错误说明)。"""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "文件不存在"
    except (OSError, UnicodeDecodeError) as exc:
        return None, "无法读取: %s" % exc
    except yaml.YAMLError as exc:
        return None, "YAML 解析失败: %s" % str(exc).splitlines()[0]


def collect(db_dir):
    """返回 (metadata, [(轮数, 案例dict)], splitting, 问题列表)。"""
    rounds, problems = [], []
    if not db_dir.is_dir():
        return None, [], None, ["目录不存在: %s" % db_dir]
    for f in sorted(db_dir.iterdir()):
        m = ROUND_RE.match(f.name)
        if not m or not f.is_file():
            continue
        data, err = load_yaml(f)
        if err or not isinstance(data, dict):
            problems.append("%s: %s" % (f.name, err or "顶层不是映射"))
            continue
        rounds.append((int(m.group(1)), data))
    rounds.sort(key=lambda item: item[0])
    meta, err = load_yaml(db_dir / "metadata.yaml")
    if err:
        problems.append("metadata.yaml: %s" % err)
    splitting, _ = load_yaml(db_dir / "splitting.yaml")
    return meta, rounds, (splitting if isinstance(splitting, dict) else None), problems


def cost_tuple(block):
    """成本块 -> (input, output, hours); 任一缺失返回 None。"""
    if not isinstance(block, dict):
        return None
    vals = [block.get(k) for k in COST_KEYS]
    if not all(is_num(v) for v in vals):
        return None
    return tuple(float(v) for v in vals)


def case_costs(data):
    """案例 costs -> {section: tuple or None} (含 total)。"""
    costs = data.get("costs") if isinstance(data.get("costs"), dict) else {}
    return {sec: cost_tuple(costs.get(sec)) for sec in COST_SECTIONS + ("total",)}


def add_cost(target, tup):
    for i in range(3):
        target[i] = (target[i] or 0) + (tup[i] or 0)


def attempts_of(data):
    opt = data.get("optimization")
    atts = opt.get("attempts") if isinstance(opt, dict) else None
    return atts if isinstance(atts, list) else []


def verdict_of(att):
    perf = att.get("performance_result")
    return perf.get("verdict") if isinstance(perf, dict) else None


def id_date(data):
    m = ID_RE.match(str(data.get("id") or ""))
    if not m:
        return None
    d = m.group(3)
    return "%s-%s-%s" % (d[:4], d[4:6], d[6:8])


def cost_consistency(rounds):
    """检查各轮 costs.total 是否等于四节之和; 返回问题列表。"""
    issues = []
    for n, data in rounds:
        c = case_costs(data)
        parts = [c[s] for s in COST_SECTIONS]
        if c["total"] and all(p is not None for p in parts):
            for i, key in enumerate(COST_KEYS):
                s = sum(p[i] for p in parts)
                if abs(s - c["total"][i]) > 1e-6:
                    issues.append("round_%d costs.total.%s=%g 与四节之和 %g 不符"
                                  % (n, key, c["total"][i], s))
        elif c["total"] is None:
            issues.append("round_%d costs.total 缺失或非数值, 报告中按四节之和计算" % n)
    return issues


def baseline_ms(meta, rounds):
    """性能基线 (ms): 优先取 metadata baselines.performance_ms, 回退首轮 before。"""
    if isinstance(meta, dict):
        baselines = meta.get("baselines")
        if isinstance(baselines, dict) and is_num(baselines.get("performance_ms")):
            return float(baselines["performance_ms"])
    if rounds:
        v = (rounds[0][1].get("final_state") or {}).get("end_to_end_before_ms")
        if is_num(v):
            return float(v)
    return None


def total_gain_ms(baseline, rounds):
    """整个优化的总收益 = 基线 - 末轮 end_to_end_after_ms (ms)。"""
    if not rounds or baseline is None:
        return None
    last = (rounds[-1][1].get("final_state") or {}).get("end_to_end_after_ms")
    if not is_num(last):
        return None
    return baseline - float(last)


def build_attempt(att, tg):
    """单优化点记录: 直读数值字段; 贡献率只对 accepted 计算。"""
    perf = att.get("performance_result") or {}
    before_ms = perf.get("before_ms") if is_num(perf.get("before_ms")) else None
    after_ms = perf.get("after_ms") if is_num(perf.get("after_ms")) else None
    gain = (before_ms - after_ms) if (before_ms is not None and after_ms is not None) else None
    verdict = verdict_of(att)
    contributable = verdict == "accepted"
    c = att.get("costs") or {}
    in_c = c.get("input_cost") if is_num(c.get("input_cost")) else None
    out_c = c.get("output_cost") if is_num(c.get("output_cost")) else None
    hours = c.get("time_hours") if is_num(c.get("time_hours")) else None
    dim = att.get("dimension")
    return {
        "attempt_index": None, "description": att.get("description"),
        "dimensions": dim if isinstance(dim, list) else ([dim] if dim else []),
        "verdict": verdict,
        "before_ms": before_ms, "after_ms": after_ms,
        "gain_ms": gain,
        "contribution_rate": (gain / tg) if (gain is not None and tg and contributable) else None,
        "verification_result": (att.get("equivalence_verification") or {}).get("result"),
        "failure_reason": att.get("failure_reason"),
        "input_cost": in_c, "output_cost": out_c, "time_hours": hours,
    }


def build_summary(meta, rounds, splitting, problems):
    """单一数据源: markdown 与 summary.json 全部由此渲染, 保证信息对齐。"""
    baseline = baseline_ms(meta, rounds)
    tg = total_gain_ms(baseline, rounds)
    notes = list(problems) + cost_consistency(rounds)
    round_list, gains_sum = [], 0.0
    counts = {"accepted": 0, "rejected": 0, "partial": 0}
    grand = [0.0, 0.0, 0.0]
    stage_sum = {s: [0.0, 0.0, 0.0] for s in COST_SECTIONS}
    for n, d in rounds:
        fs = d.get("final_state") or {}
        attempts = []
        for i, att in enumerate(attempts_of(d), 1):
            if not isinstance(att, dict):
                continue
            rec = build_attempt(att, tg)
            rec["attempt_index"] = i
            attempts.append(rec)
            if rec["verdict"] in counts:
                counts[rec["verdict"]] += 1
            if rec["contribution_rate"] is not None:
                gains_sum += rec["gain_ms"]
        c = case_costs(d)
        for sec in COST_SECTIONS:
            if c[sec]:
                add_cost(grand, c[sec])
                add_cost(stage_sum[sec], c[sec])
        nb = fs.get("end_to_end_before_ms") if is_num(fs.get("end_to_end_before_ms")) else None
        na = fs.get("end_to_end_after_ms") if is_num(fs.get("end_to_end_after_ms")) else None
        total = c["total"] or (tuple(sum(x) for x in zip(*[c[s] for s in COST_SECTIONS]))
                               if all(c[s] for s in COST_SECTIONS) else None)
        round_list.append({
            "round": n, "case_id": d.get("id"), "date": id_date(d),
            "end_to_end_before_ms": nb, "end_to_end_after_ms": na,
            "round_speedup": (nb / na) if (nb and na) else None,
            "counts": {"attempts": len(attempts),
                       "accepted": sum(1 for a in attempts if a["verdict"] == "accepted"),
                       "rejected": sum(1 for a in attempts if a["verdict"] == "rejected"),
                       "partial": sum(1 for a in attempts if a["verdict"] == "partial")},
            "costs": {sec: dict(zip(COST_KEYS, c[sec])) if c[sec] else None
                      for sec in COST_SECTIONS},
            "costs_total": dict(zip(COST_KEYS, total)) if total else None,
            "remaining_bottleneck": fs.get("remaining_bottleneck"),
            "attempts": attempts,
        })
    for r in round_list:
        r["cumulative_speedup"] = (baseline / r["end_to_end_after_ms"]) \
            if (baseline and r["end_to_end_after_ms"]) else None
    if rounds:
        last_after = round_list[-1]["end_to_end_after_ms"]
        e2e = {"baseline_ms": baseline,
               "after_ms": last_after,
               "speedup": (baseline / last_after) if (baseline and last_after) else None,
               "gain_ms": tg}
        if last_after is None:
            notes.append("末轮 end_to_end_after_ms 缺失或非数值, 未计算总加速比/贡献率")
    else:
        e2e = {"baseline_ms": baseline, "after_ms": None,
               "speedup": None, "gain_ms": None}
    return {
        "generated": date.today().isoformat(),
        "metadata": meta,
        "rounds": round_list,
        "aggregate": {
            "end_to_end": e2e,
            "counts": dict(counts, rounds=len(rounds), attempts=sum(counts.values())),
            "attempts_gain_sum": gains_sum,
            "costs": {sec: dict(zip(COST_KEYS, stage_sum[sec])) for sec in COST_SECTIONS},
            "costs_grand": dict(zip(COST_KEYS, grand)),
            "remaining_bottleneck": (round_list[-1]["remaining_bottleneck"]
                                    if round_list else None),
        },
        "splitting": splitting,
        "notes": notes,
    }


def render_markdown(summary):
    """从 build_summary 的数据结构渲染人读报告 (与 summary.json 同源)。"""
    meta = summary["metadata"] if isinstance(summary["metadata"], dict) else {}
    model = meta.get("model") if isinstance(meta.get("model"), dict) else {}
    agg = summary["aggregate"]
    out = []
    out.append("# 优化总结报告 — %s" % (model.get("name") or "未知名 (metadata 缺失)"))
    out.append("")
    out.append("> 生成日期: %s · 数据来源: evidence_db/ (metadata.yaml + %d 轮案例) · "
               "脚本: model_opt/06_evidence_db/scripts/generate_summary.py"
               % (summary["generated"], agg["counts"]["rounds"]))
    out.append("")

    out.append("## 1. 模型与环境")
    out.append("")
    out.append("| 项 | 值 |")
    out.append("|---|---|")
    for section, fields in META_FIELDS:
        block = meta.get(section)
        block = block if isinstance(block, dict) else {}
        for f in fields:
            out.append("| %s | %s |" % (META_LABELS[(section, f)],
                                       str(block.get(f) or "—").replace("|", "\\|")))
    out.append("")

    cnt, e2e = agg["counts"], agg["end_to_end"]
    out.append("## 2. 优化总览")
    out.append("")
    out.append("- 优化轮次: **%d** 轮, 优化点共 **%d** 个 (accepted %d / rejected %d / partial %d)"
               % (cnt["rounds"], cnt["attempts"], cnt["accepted"], cnt["rejected"], cnt["partial"]))
    out.append("- 端到端效果: **%s** → **%s**" % (fmt(e2e["baseline_ms"], "ms"),
                                                 fmt(e2e["after_ms"], "ms")))
    if e2e["speedup"]:
        out.append("- 端到端总加速比: **%.2fx** (基线 / 末轮)" % e2e["speedup"])
        out.append("- 端到端总收益: **%s**" % fmt(e2e["gain_ms"], "ms"))
    if agg["remaining_bottleneck"]:
        out.append("- 剩余瓶颈: %s" % agg["remaining_bottleneck"])
    out.append("")
    out.append("### 各轮一览")
    out.append("")
    out.append("| 轮次 | 日期 | 优化点 | 采纳/拒绝/部分 | 端到端 before → after (ms) | 轮内加速比 | 累计加速比 (vs 基线) |")
    out.append("|---|---|---|---|---|---|---|")
    for r in summary["rounds"]:
        rc = r["counts"]
        sp = "%.2fx" % r["round_speedup"] if r["round_speedup"] else "—"
        csp = "%.3fx" % r["cumulative_speedup"] if r["cumulative_speedup"] else "—"
        out.append("| %d | %s | %d | %d/%d/%d | %s → %s | %s | %s |"
                   % (r["round"], r["date"] or "—", rc["attempts"],
                      rc["accepted"], rc["rejected"], rc["partial"],
                      fmt(r["end_to_end_before_ms"]), fmt(r["end_to_end_after_ms"]),
                      sp, csp))
    out.append("")

    out.append("## 3. 优化点明细")
    out.append("")
    for r in summary["rounds"]:
        out.append("### Round %d (%s)" % (r["round"], r["date"] or "—"))
        out.append("")
        out.append("| # | 优化点 | 维度 | 判定 | 单独收益 (before_ms → after_ms) | 收益 Δ | 贡献率 | 验证 | 成本 (M in / M out / h) | 失败原因 |")
        out.append("|---|---|---|---|---|---|---|---|---|---|")
        for a in r["attempts"]:
            dim_s = ", ".join(str(x) for x in a["dimensions"])
            contrib = ("%.1f%%" % (a["contribution_rate"] * 100)
                       if a["contribution_rate"] is not None else "—")
            cost_s = " / ".join(fmt(a[k]) for k in ("input_cost", "output_cost", "time_hours"))
            out.append("| %d | %s | %s | %s | %s → %s | %s | %s | %s | %s | %s |"
                       % (a["attempt_index"],
                          str(a["description"] or "—").replace("\n", " "),
                          dim_s or "—", a["verdict"] or "—",
                          fmt(a["before_ms"]), fmt(a["after_ms"]),
                          fmt(a["gain_ms"], "ms"), contrib,
                          a["verification_result"] or "—", cost_s,
                          str(a["failure_reason"] or "—").replace("\n", " ")))
        out.append("")
    if e2e["gain_ms"]:
        out.append("> 贡献率合计: **%.1f%%** (总收益 %s ms; 仅统计 accepted)——"
                   "偏离 100%% 的部分来自优化点间交互效应与各点单独 A/B 的口径差"
                   % (agg["attempts_gain_sum"] / e2e["gain_ms"] * 100, fmt(e2e["gain_ms"])))
        out.append("")

    out.append("## 4. 成本汇总")
    out.append("")
    out.append("| 轮次 | 阶段 | 输入 (M tok) | 输出 (M tok) | 耗时 (h) |")
    out.append("|---|---|---|---|---|")
    for r in summary["rounds"]:
        for sec in COST_SECTIONS:
            c = r["costs"][sec]
            out.append("| %d | %s | %s | %s | %s |"
                       % (r["round"], COST_LABELS[sec],
                          fmt(c["input_cost"]) if c else "—",
                          fmt(c["output_cost"]) if c else "—",
                          fmt(c["time_hours"]) if c else "—"))
        t = r["costs_total"]
        if t:
            out.append("| %d | **小计** | **%s** | **%s** | **%s** |"
                       % (r["round"], fmt(t["input_cost"]),
                          fmt(t["output_cost"]), fmt(t["time_hours"])))
    g = agg["costs_grand"]
    out.append("| **总计** | 全部 | **%s** | **%s** | **%s** |"
               % (fmt(g["input_cost"]), fmt(g["output_cost"]), fmt(g["time_hours"])))
    out.append("")
    out.append("| 阶段合计 | 输入 (M tok) | 输出 (M tok) | 耗时 (h) |")
    out.append("|---|---|---|---|")
    for sec in COST_SECTIONS:
        s = agg["costs"][sec]
        out.append("| %s | %s | %s | %s |"
                   % (COST_LABELS[sec], fmt(s["input_cost"]),
                      fmt(s["output_cost"]), fmt(s["time_hours"])))
    out.append("")

    if summary["splitting"]:
        ps = summary["splitting"].get("parallel_splitting") or {}
        out.append("## 5. 多卡切分")
        out.append("")
        out.append("- 切分案例: %s" % (summary["splitting"].get("id") or "—"))
        for key, label in (("split_position", "接入位置"), ("split_dimension", "切分维度"),
                           ("implementation_mode", "实现机制"), ("rollback", "回退机制")):
            out.append("- %s: %s" % (label, ps.get(key) or "—"))
        ver = ps.get("verification") or {}
        out.append("- 验证: %s | %s" % (ver.get("method") or "—", ver.get("result") or "—"))
        sc = summary["splitting"].get("costs") or {}
        out.append("- 切分成本: %s M in / %s M out / %s h"
                   % (fmt(sc.get("input_cost")), fmt(sc.get("output_cost")),
                      fmt(sc.get("time_hours"))))
        out.append("")

    if summary["notes"]:
        out.append("## 6. 数据说明")
        out.append("")
        for note in summary["notes"]:
            out.append("- %s" % note)
        out.append("")
    return "\n".join(out)


def write_outputs(db, summary, md_text, out_md):
    out_md.write_text(md_text, encoding="utf-8")
    json_path = db / "summary.json"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return out_md, json_path


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="优化总结报告生成 (summary.md 人读 + summary.json 机器镜像)")
    ap.add_argument("db_dir", help="evidence_db 目录")
    ap.add_argument("--output", "-o", default=None,
                    help="markdown 输出路径 (默认 <db>/summary.md; "
                         "summary.json 固定生成于 db 目录)")
    args = ap.parse_args(argv)
    db = Path(args.db_dir)
    meta, rounds, splitting, problems = collect(db)
    summary = build_summary(meta, rounds, splitting, problems)
    md = render_markdown(summary)
    out_md = Path(args.output) if args.output else db / "summary.md"
    out_md.parent.mkdir(parents=True, exist_ok=True)
    outputs = write_outputs(db, summary, md, out_md)
    print("markdown 报告: %s" % outputs[0])
    print("json 镜像: %s" % outputs[1])
    if summary["notes"]:
        print("注意: %d 个数据问题已写入报告「数据说明」" % len(summary["notes"]),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
