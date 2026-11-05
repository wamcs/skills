#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evidence_db 案例校验脚本 (06_evidence_db, 门禁 B).

严格对照同目录 schema.md 校验 <workspace>/evidence_db/ 下的案例文件:
只检查内容合法性 (结构/枚举/格式/链路) 与信息覆盖度 (必填字段齐全)。
非规范一律打回, 不做容错适配。

  - 结构: YAML 可解析、无重复 key、无残留占位符、顶层为案例映射 (每文件一个案例)
  - 命名: 文件名 = round_<N>.yaml; id = <模型名称>-round_<N>-<日期YYYYMMDD>; 轮数与文件名一致
  - 必填字段: phenomenon.signals[] (id/source/content, 只记被分析线引用者) /
          analysis_path[] 多分析线 (id/from_signals/steps/conclusion) / root_cause /
          optimization.attempts (含 from_analysis 与 costs) / final_state / costs
  - 数值格式: performance_result.before_ms/after_ms 须为数值, accepted 必填且
          after <= before (性能回退打回); final_state.end_to_end_before_ms/after_ms
          须为 float (单位固定 ms)
  - 成本: 各优化点 costs{input_cost/output_cost/time_hours} + 案例级五节
          {analysis/optimization/debugging/system/total}, total 须等于四节之和 (算术校验)
  - 封闭世界: 所有映射只允许 schema 定义字段, 未知字段一律打回
  - 链路闭合: from_signals 只能引用存在的信号 id; from_analysis 只能引用存在的分析线 id;
          未被任何分析线引用的信号应剔除; 前置排除项不进 attempts
  - 枚举严格匹配: verdict / dimension / bottleneck_type
  - 轮间一致性: 轮次 1..N 连续; 首轮 end_to_end_before_ms = metadata 基线
          baselines.performance_ms; 本轮 before = 上一轮 after (链式);
          同轮 accepted 不得有完全相同的 (before_ms, after_ms) (重复计账拦截)
  - 目录级: evidence_db/metadata.yaml (准备阶段生成, 模板见同目录 metadata.md) 须存在且四节完整;
          splitting.yaml (切分案例, 如存在) 单独校验——parallel_splitting 节 + 链路 + costs

用法:
  python3 validate_evidence.py <evidence_db 目录或案例文件>...
退出码: 0 = 通过, 1 = 存在不合规, 2 = 路径错误
"""

import argparse
import os
import re
import sys
from pathlib import Path

import yaml

BOTTLENECK_TYPES = {"Host-Bound", "Compute-Bound", "Memory-Bound",
                    "Allocator-Bound", "Execution-Mode", "mixed"}
DIMENSIONS = {"eliminate_redundancy", "reuse_and_precompute",
              "hide_latency", "equivalent_substitution"}
VERDICTS = {"accepted", "rejected", "partial"}
PLACEHOLDER_RE = re.compile(r"[(（]AI\s*填写|\bTBD\b")
ID_RE = re.compile(r"^(.+)-round_(\d+)-(\d{8})$")
FILE_RE = re.compile(r"^round_(\d+)\.yaml$")
TOP_KEYS = {"id", "profiling_ref", "phenomenon", "analysis_path", "root_cause",
            "optimization", "final_state", "costs", "notes"}
SPLIT_KEYS = {"id", "profiling_ref", "phenomenon", "analysis_path", "root_cause",
              "parallel_splitting", "costs", "notes"}
SPLIT_ID_RE = re.compile(r"^(.+)-splitting-(\d{8})$")
EXCLUDED_FILES = {"metadata.yaml", "splitting.yaml", "summary.md", "summary.json",
                  "summary.jsonl", "summary.csv"}
SIGNAL_KEYS = {"id", "source", "content"}
PATH_KEYS = {"id", "from_signals", "steps", "conclusion"}
ATT_KEYS = {"description", "from_analysis", "dimension", "implementation_detail",
            "costs", "equivalence_verification", "performance_result", "failure_reason"}
PERF_KEYS = {"metric", "baseline_ref", "before_ms", "after_ms", "verdict"}
COST_KEYS = ("input_cost", "output_cost", "time_hours")
COST_SECTIONS = ("analysis", "optimization", "debugging", "system")
FS_KEYS = {"adopted", "files_modified", "end_to_end_before_ms", "end_to_end_after_ms",
           "remaining_bottleneck"}
RC_KEYS = {"description", "bottleneck_type", "evidence"}
EQ_KEYS = {"method", "metrics", "result"}
METRIC_KEYS = {"metric", "threshold", "value"}
META_FIELDS = (("model", ("name", "domain", "architecture", "params")),
               ("test_data", ("characteristics", "input_spec")),
               ("baselines", ("performance", "performance_ms", "accuracy")),
               ("environment", ("hardware", "software_stack")))


class DuplicateKeyError(Exception):
    pass


class StrictLoader(yaml.SafeLoader):
    """SafeLoader + 任意层级重复 key 检测 (默认 PyYAML 静默后者覆盖前者)."""

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        seen = {}
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            line = key_node.start_mark.line + 1
            try:
                if key in seen:
                    raise DuplicateKeyError(
                        "重复 key '%s': line %d 与 line %d" % (key, seen[key], line))
                seen[key] = line
            except TypeError:
                continue
        return super().construct_mapping(node, deep)


class CaseFile:
    def __init__(self, path):
        self.path = Path(path)
        self.errors = []

    def err(self, msg):
        self.errors.append(msg)


def nonempty(value):
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def req(cf, mapping, key, path):
    """必填字段: 存在且非空。返回字段值 (缺损失败时 None)。"""
    if not isinstance(mapping, dict):
        return None
    if key not in mapping:
        cf.err("缺少必填字段 %s" % path)
        return None
    if not nonempty(mapping[key]):
        cf.err("必填字段 %s 为空" % path)
        return None
    return mapping[key]


def check_closed_keys(cf, mapping, allowed, path):
    """封闭世界校验: 只允许 schema 定义的字段,未知字段一律打回。"""
    for k in mapping:
        if k not in allowed:
            cf.err("%s 含未定义字段 '%s'" % (path, k))


def attempts_of(case):
    opt = case.get("optimization")
    atts = opt.get("attempts") if isinstance(opt, dict) else None
    return atts if isinstance(atts, list) else []


def validate_signals(cf, case):
    """返回信号 id 列表 (供引用与未引用校验)。"""
    ph = case.get("phenomenon")
    if not isinstance(ph, dict):
        cf.err("缺少必填字段 phenomenon")
        return []
    signals = ph.get("signals")
    if not isinstance(signals, list) or not signals:
        cf.err("phenomenon.signals 缺失或为空")
        return []
    ids = []
    for i, sig in enumerate(signals):
        p = "phenomenon.signals[%d]" % i
        if not isinstance(sig, dict):
            cf.err("%s 不是映射" % p)
            continue
        sid = req(cf, sig, "id", p + ".id")
        req(cf, sig, "source", p + ".source")
        req(cf, sig, "content", p + ".content")
        for k in sig:
            if k not in SIGNAL_KEYS:
                cf.err("%s 含未定义字段 '%s'" % (p, k))
        if sid is not None:
            if str(sid) in ids:
                cf.err("%s.id '%s' 与其他信号重复" % (p, sid))
            ids.append(str(sid))
    return ids


def validate_paths(cf, case, signal_ids):
    """analysis_path 须为分析线列表; 返回 (分析线 id 集合, 被引用的信号 id 集合)。"""
    ap = case.get("analysis_path")
    if not isinstance(ap, list) or not ap:
        cf.err("analysis_path 缺失或非分析线列表 (v2: 每条分析线一个条目, "
               "含 id/from_signals/steps/conclusion)")
        return set(), set()
    ids, referenced = set(), set()
    for i, path in enumerate(ap):
        p = "analysis_path[%d]" % i
        if not isinstance(path, dict):
            cf.err("%s 不是映射" % p)
            continue
        pid = req(cf, path, "id", p + ".id")
        frm = req(cf, path, "from_signals", p + ".from_signals")
        for k in path:
            if k not in PATH_KEYS:
                cf.err("%s 含未定义字段 '%s' (profiling_ref 为案例顶层字段)" % (p, k))
        if frm is not None:
            if not isinstance(frm, list) or not frm:
                cf.err("%s.from_signals 应为非空 id 列表" % p)
            else:
                for sid in frm:
                    referenced.add(str(sid))
                    if str(sid) not in signal_ids:
                        cf.err("%s.from_signals 引用了不存在的信号 id '%s'" % (p, sid))
        steps = path.get("steps")
        if not isinstance(steps, list) or not steps:
            cf.err("%s.steps 缺失或为空" % p)
        else:
            for j, st in enumerate(steps):
                sp = "%s.steps[%d]" % (p, j)
                if not isinstance(st, dict):
                    cf.err("%s 不是映射" % sp)
                    continue
                req(cf, st, "action", sp + ".action")
                req(cf, st, "observation", sp + ".observation")
        req(cf, path, "conclusion", p + ".conclusion")
        if pid is not None:
            if str(pid) in ids:
                cf.err("%s.id '%s' 与其他分析线重复" % (p, pid))
            ids.add(str(pid))
    return ids, referenced

def validate_root_cause(cf, case):
    rc = case.get("root_cause")
    if not isinstance(rc, dict):
        cf.err("缺少必填字段 root_cause")
        return
    check_closed_keys(cf, rc, RC_KEYS, "root_cause")
    req(cf, rc, "description", "root_cause.description")
    req(cf, rc, "evidence", "root_cause.evidence")
    bt = rc.get("bottleneck_type")
    if bt is not None and str(bt) not in BOTTLENECK_TYPES:
        cf.err("root_cause.bottleneck_type '%s' 不在枚举内 "
               "(补充说明写入 description, 多种写 mixed)" % bt)


def check_flat_costs(cf, case):
    """切分案例成本: 扁平三字段 (无优化循环的阶段划分)。"""
    costs = case.get("costs")
    if not isinstance(costs, dict):
        cf.err("缺少必填字段 costs (input_cost/output_cost/time_hours)")
        return
    check_closed_keys(cf, costs, set(COST_KEYS), "costs")
    for f in COST_KEYS:
        if not is_number(costs.get(f)):
            cf.err("costs.%s 缺失或非数值 (token 单位 Million, 时间单位小时)" % f)


def check_cost_block(cf, block, path):
    """优化案例各节成本: 三字段 + 封闭世界。"""
    check_closed_keys(cf, block, set(COST_KEYS), path)
    for f in COST_KEYS:
        if not is_number(block.get(f)):
            cf.err("%s.%s 缺失或非数值 (token 单位 Million, 时间单位小时)" % (path, f))


def validate_costs(cf, case):
    costs = case.get("costs")
    if not isinstance(costs, dict):
        cf.err("缺少必填字段 costs (analysis/optimization/debugging/system/total)")
        return
    check_closed_keys(cf, costs, set(COST_SECTIONS) | {"total"}, "costs")
    blocks = []
    for sec in COST_SECTIONS:
        block = costs.get(sec)
        if not isinstance(block, dict):
            cf.err("costs.%s 缺失或非映射" % sec)
            blocks.append(None)
            continue
        check_cost_block(cf, block, "costs.%s" % sec)
        blocks.append(block)
    total = costs.get("total")
    if not isinstance(total, dict):
        cf.err("costs.total 缺失或非映射")
        return
    check_cost_block(cf, total, "costs.total")
    if all(b is not None for b in blocks):
        for key in COST_KEYS:
            s = sum(float(b[key]) for b in blocks)
            if abs(s - float(total[key])) > 1e-6:
                cf.err("costs.total.%s=%s 与四节之和 %g 不符 (total 须等于 "
                       "analysis+optimization+debugging+system)" % (key, total[key], s))


def validate_optimization(cf, case, path_ids):
    opt = case.get("optimization")
    if not (isinstance(opt, dict) and nonempty(opt.get("attempts"))):
        cf.err("缺少 optimization.attempts (切分案例见 splitting.yaml, 不在轮次案例内)")
        return
    for i, att in enumerate(opt["attempts"]):
        p = "optimization.attempts[%d]" % i
        if not isinstance(att, dict):
            cf.err("%s 不是映射" % p)
            continue
        req(cf, att, "description", p + ".description")
        for k in att:
            if k not in ATT_KEYS:
                cf.err("%s 含未定义字段 '%s'" % (p, k))
        fa = req(cf, att, "from_analysis", p + ".from_analysis")
        if fa is not None:
            if not isinstance(fa, list) or not fa:
                cf.err("%s.from_analysis 应为非空 id 列表" % p)
            else:
                for pid in fa:
                    if str(pid) not in path_ids:
                        cf.err("%s.from_analysis 引用了不存在的分析线 id '%s'" % (p, pid))
        dim = att.get("dimension")
        if dim is not None:
            if not isinstance(dim, list):
                cf.err("%s.dimension 应为维度列表(可多选)" % p)
            elif dim:  # 非空列表才检查枚举; 空列表等价于省略
                for d in dim:
                    if str(d) not in DIMENSIONS:
                        cf.err("%s.dimension 含非枚举项 '%s'" % (p, d))
        req(cf, att, "implementation_detail", p + ".implementation_detail")
        att_costs = att.get("costs")
        if not isinstance(att_costs, dict):
            cf.err("%s.costs 缺失 (该优化点成本: 方案设计+实施+调试)" % p)
        else:
            check_cost_block(cf, att_costs, "%s.costs" % p)
        eq = att.get("equivalence_verification")
        if not isinstance(eq, dict):
            cf.err("%s.equivalence_verification 缺失" % p)
        else:
            check_closed_keys(cf, eq, EQ_KEYS, "%s.equivalence_verification" % p)
            req(cf, eq, "method", p + ".equivalence_verification.method")
            req(cf, eq, "result", p + ".equivalence_verification.result")
            mets = eq.get("metrics")
            if mets is not None:
                if not isinstance(mets, list):
                    cf.err("%s.equivalence_verification.metrics 应为列表" % p)
                else:
                    for j, m in enumerate(mets):
                        ok = (isinstance(m, dict) and nonempty(m.get("metric"))
                              and nonempty(m.get("threshold")) and nonempty(m.get("value")))
                        if not ok:
                            cf.err("%s.equivalence_verification.metrics[%d] "
                                   "字段不全 (metric/threshold/value)" % (p, j))
                        else:
                            check_closed_keys(cf, m, METRIC_KEYS,
                                              "%s.equivalence_verification.metrics[%d]" % (p, j))
        perf = att.get("performance_result")
        if not isinstance(perf, dict):
            cf.err("%s.performance_result 缺失" % p)
        else:
            check_closed_keys(cf, perf, PERF_KEYS, "%s.performance_result" % p)
            req(cf, perf, "metric", "%s.performance_result.metric" % p)
            bms = perf.get("before_ms")
            if not is_number(bms):
                cf.err("%s.performance_result.before_ms 缺失或非数值 "
                       "(执行过的优化点必须有量化 A/B; 前置排除项不进 attempts)" % p)
            ams = perf.get("after_ms")
            verdict = perf.get("verdict")
            if verdict is not None and str(verdict) not in VERDICTS:
                cf.err("%s.performance_result.verdict '%s' 不在枚举 "
                       "accepted/rejected/partial 内 (细节写入 result/failure_reason)"
                       % (p, verdict))
            if verdict == "accepted":
                if not is_number(ams):
                    cf.err("%s.performance_result.after_ms 缺失或非数值 "
                           "(accepted 必填量化 A/B)" % p)
                elif is_number(bms) and ams > bms:
                    cf.err("%s accepted 但 after_ms(%s) > before_ms(%s)——性能回退应记 "
                           "rejected + failure_reason" % (p, ams, bms))
            if verdict == "rejected" and not nonempty(att.get("failure_reason")):
                cf.err("%s verdict=rejected 但缺 failure_reason (失败必记)" % p)


def validate_final_state(cf, case):
    fs = case.get("final_state")
    if not isinstance(fs, dict):
        cf.err("缺少必填字段 final_state")
        return
    check_closed_keys(cf, fs, FS_KEYS, "final_state")
    req(cf, fs, "adopted", "final_state.adopted")
    fm = fs.get("files_modified")
    if fm is not None and not (isinstance(fm, list) and all(isinstance(x, str) for x in fm)):
        cf.err("final_state.files_modified 应为字符串列表")
    for f in ("end_to_end_before_ms", "end_to_end_after_ms"):
        if not is_number(fs.get(f)):
            cf.err("final_state.%s 缺失或非数值 (float, 单位固定 ms)" % f)


def check_metadata(dir_path):
    """校验 evidence_db 目录下的 metadata.yaml (准备阶段生成, 全轮共用)。"""
    cf = CaseFile(dir_path / "metadata.yaml")
    fp = dir_path / "metadata.yaml"
    if not fp.is_file():
        cf.err("缺少 metadata.yaml (准备阶段生成, 模板见 06_evidence_db/metadata.md)")
        return cf
    try:
        data = yaml.safe_load(fp.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        cf.err("metadata.yaml 无法读取: %s" % exc)
        return cf
    except yaml.YAMLError as exc:
        cf.err("metadata.yaml YAML 解析失败: %s" % str(exc).splitlines()[0])
        return cf
    if not isinstance(data, dict):
        cf.err("metadata.yaml 顶层应为映射")
        return cf
    check_closed_keys(cf, data, {s for s, _ in META_FIELDS}, "metadata.yaml")
    for section, fields in META_FIELDS:
        block = data.get(section)
        if not isinstance(block, dict):
            cf.err("metadata.yaml 缺少 '%s' 节" % section)
            continue
        check_closed_keys(cf, block, set(fields), "metadata.yaml.%s" % section)
        for f in fields:
            if not nonempty(block.get(f)):
                cf.err("metadata.yaml %s.%s 缺失或为空" % (section, f))
    return cf


def validate_parallel_splitting(cf, ps):
    for f in ("split_position", "split_dimension", "implementation_mode", "rollback",
              "tensor_distribution_table", "quantitative", "proofs", "verification"):
        req(cf, ps, f, "parallel_splitting.%s" % f)
    tdt = ps.get("tensor_distribution_table")
    if not isinstance(tdt, list):
        cf.err("parallel_splitting.tensor_distribution_table 应为列表")
    else:
        for i, t in enumerate(tdt):
            if isinstance(t, dict):
                for f in ("tensor", "global_shape", "distribution", "per_card_shape",
                          "consumer_op", "recovery_comm"):
                    if not nonempty(t.get(f)):
                        cf.err("parallel_splitting.tensor_distribution_table[%d].%s "
                               "缺失或为空 (未声明 = 潜在 bug)" % (i, f))
    ver = ps.get("verification")
    if not isinstance(ver, dict):
        cf.err("parallel_splitting.verification 应为映射")
    else:
        req(cf, ver, "method", "parallel_splitting.verification.method")
        req(cf, ver, "result", "parallel_splitting.verification.result")
    q = ps.get("quantitative")
    if not isinstance(q, dict):
        cf.err("parallel_splitting.quantitative 应为映射")
    else:
        for f in ("single_card_peak_bytes", "per_card_peak_after_split_bytes", "hbm_bytes",
                  "communication_volume_bytes", "communication_time_ms", "break_even"):
            if not nonempty(q.get(f)):
                cf.err("parallel_splitting.quantitative.%s 缺失或为空" % f)
    proofs = ps.get("proofs")
    if not isinstance(proofs, list):
        cf.err("parallel_splitting.proofs 应为列表")
    else:
        for i, pr in enumerate(proofs):
            if isinstance(pr, dict):
                for f in ("operation", "equivalence_proof", "lower_bound_comm_bytes",
                          "actual_comm_bytes", "ratio"):
                    if not nonempty(pr.get(f)):
                        cf.err("parallel_splitting.proofs[%d].%s 缺失或为空" % (i, f))


def parse_case_file(cf, path):
    """读 + 占位符扫描 + 严格加载; 返回案例 dict 或 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        cf.err("无法读取: %s" % exc)
        return None
    for lineno, line in enumerate(text.splitlines(), 1):
        if PLACEHOLDER_RE.search(line):
            cf.err("line %d 残留未填写占位符: %s" % (lineno, line.strip()[:60]))
    try:
        try:
            data = yaml.load(text, Loader=StrictLoader)
        except DuplicateKeyError as exc:
            cf.err("YAML %s" % exc)
            data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        cf.err("YAML 解析失败: %s" % str(exc).splitlines()[0])
        return None
    if data is None:
        cf.err("文件内容为空")
        return None
    if not isinstance(data, dict):
        cf.err("顶层应为案例映射, 实际为 %s" % type(data).__name__)
        return None
    return data


def common_linkage(cf, case):
    """phenomenon -> analysis_path 链路公共校验; 返回分析线 id 集合。"""
    signal_ids = validate_signals(cf, case)
    path_ids, referenced = validate_paths(cf, case, signal_ids)
    for sid in signal_ids:
        if sid not in referenced:
            cf.err("phenomenon.signals 含未被任何分析线引用的信号 '%s' "
                   "(只记与案例相关的信号)" % sid)
    return path_ids


def check_file(path):
    cf = CaseFile(path)
    if path.suffix.lower() not in (".yaml", ".yml"):
        cf.err("案例文件须为 YAML (schema 目录结构: round_<N>.yaml), 实际后缀 '%s'" % path.suffix)
        return cf
    case = parse_case_file(cf, path)
    if case is None:
        return cf
    cid = req(cf, case, "id", "id")
    m = ID_RE.match(str(cid)) if cid is not None else None
    if cid is not None and not m:
        cf.err("id '%s' 不符合格式 <模型名称>-round_<N>-<日期YYYYMMDD>" % cid)
    fm = FILE_RE.match(path.name)
    if not fm:
        cf.err("文件名 '%s' 不符合 round_<N>.yaml 约定" % path.name)
    elif m and fm.group(1) != m.group(2):
        cf.err("文件名轮数 round_%s 与 id 轮数 round_%s 不一致" % (fm.group(1), m.group(2)))
    if "profiling_ref" in case and not nonempty(case["profiling_ref"]):
        cf.err("profiling_ref 为空")
    for k in case:
        if k not in TOP_KEYS:
            cf.err("顶层字段 '%s' 不在 schema 定义中 (信息应并入对应节)" % k)
    path_ids = common_linkage(cf, case)
    validate_root_cause(cf, case)
    validate_optimization(cf, case, path_ids)
    validate_final_state(cf, case)
    validate_costs(cf, case)
    return cf


def check_splitting_file(path):
    """校验切分案例文件 splitting.yaml。"""
    cf = CaseFile(path)
    case = parse_case_file(cf, path)
    if case is None:
        return cf
    cid = req(cf, case, "id", "id")
    if cid is not None and not SPLIT_ID_RE.match(str(cid)):
        cf.err("id '%s' 不符合格式 <模型名称>-splitting-<日期YYYYMMDD>" % cid)
    for k in case:
        if k not in SPLIT_KEYS:
            cf.err("顶层字段 '%s' 不在切分案例 schema 定义中 "
                   "(切分案例无 optimization/final_state)" % k)
    common_linkage(cf, case)
    validate_root_cause(cf, case)
    ps = case.get("parallel_splitting")
    if not isinstance(ps, dict):
        cf.err("缺少必填字段 parallel_splitting")
    else:
        validate_parallel_splitting(cf, ps)
    check_flat_costs(cf, case)
    return cf


def cross_round_checks(db_dir, round_cases, meta_data):
    """轮间一致性: 轮次连续 + 首轮对齐基线 + 链式衔接 + 同轮重复计账。"""
    cf = CaseFile(db_dir / "(跨轮一致性)")
    nums = [n for n, _ in round_cases]
    if nums and nums != list(range(1, len(nums) + 1)):
        cf.err("轮次不连续: %s (应为 1..%d 连续)" % (nums, len(nums)))
    if round_cases:
        first_before = (round_cases[0][1].get("final_state") or {}).get("end_to_end_before_ms")
        baseline = None
        if isinstance(meta_data, dict):
            baselines = meta_data.get("baselines")
            if isinstance(baselines, dict) and is_number(baselines.get("performance_ms")):
                baseline = float(baselines["performance_ms"])
        if baseline is None:
            cf.err("metadata.yaml baselines.performance_ms 缺失或非数值——"
                   "无法校验首轮基线对齐")
        elif is_number(first_before):
            tol = max(0.05, baseline * 0.001)
            if abs(baseline - float(first_before)) > tol:
                cf.err("round_1 end_to_end_before_ms(%g) 与 metadata 基线 "
                       "performance_ms(%g) 不符——首轮 before 必须等于原始基线"
                       % (first_before, baseline))
    for prev, cur in zip(round_cases, round_cases[1:]):
        prev_after = (prev[1].get("final_state") or {}).get("end_to_end_after_ms")
        cur_before = (cur[1].get("final_state") or {}).get("end_to_end_before_ms")
        if is_number(prev_after) and is_number(cur_before):
            tol = max(0.05, abs(float(prev_after)) * 0.001)
            if abs(float(prev_after) - float(cur_before)) > tol:
                cf.err("round_%d end_to_end_before_ms(%g) 与 round_%d 终态 "
                       "after_ms(%g) 不衔接——链式口径要求本轮 before = 上一轮 after"
                       % (cur[0], cur_before, prev[0], prev_after))
    for n, d in round_cases:
        seen = {}
        for att in attempts_of(d):
            if not isinstance(att, dict):
                continue
            perf = att.get("performance_result") or {}
            if perf.get("verdict") != "accepted":
                continue
            bms, ams = perf.get("before_ms"), perf.get("after_ms")
            if not (is_number(bms) and is_number(ams)):
                continue
            key = (float(bms), float(ams))
            desc = str(att.get("description"))[:30]
            if key in seen:
                cf.err("round_%d '%s' 与 '%s' 的 (before_ms, after_ms) 完全相同——"
                       "重复计账嫌疑, 须分开测量各自单独 A/B 或合并为一条"
                       % (n, seen[key], desc))
            else:
                seen[key] = desc
    return cf


def main(argv=None):
    ap = argparse.ArgumentParser(description="evidence_db 案例 schema 校验 (门禁 B)")
    ap.add_argument("targets", nargs="+", help="evidence_db 目录或案例文件")
    args = ap.parse_args(argv)
    files, dirs = [], []
    for t in args.targets:
        p = Path(t)
        if p.is_dir():
            files += sorted(x for x in p.iterdir()
                            if x.is_file() and not x.name.startswith(".")
                            and x.name not in EXCLUDED_FILES)
            dirs.append(p)
        elif p.is_file():
            files.append(p)
        else:
            print("路径不存在: %s" % p, file=sys.stderr)
            return 2
    results = [check_file(f) for f in files]
    for d in dict.fromkeys(dirs):
        results.append(check_metadata(d))
        if (d / "splitting.yaml").is_file():
            results.append(check_splitting_file(d / "splitting.yaml"))
        round_cases = []
        for f in sorted(d.iterdir()):
            m = FILE_RE.match(f.name)
            if not (m and f.is_file()):
                continue
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
            except Exception:
                data = None
            if isinstance(data, dict):
                round_cases.append((int(m.group(1)), data))
        round_cases.sort(key=lambda x: x[0])
        try:
            meta_data = yaml.safe_load((d / "metadata.yaml").read_text(encoding="utf-8"))
        except Exception:
            meta_data = None
        results.append(cross_round_checks(d, round_cases, meta_data))
    common = os.path.commonpath([str(r.path) for r in results]) if results else ""
    for r in results:
        rel = os.path.relpath(str(r.path), common) if common else str(r.path)
        if rel == ".":
            rel = str(r.path)
        if not r.errors:
            print("[OK]   %s" % rel)
        else:
            print("[FAIL] %s" % rel)
            for msg in r.errors:
                print("       ERROR %s" % msg)
    groups = {}
    for r in results:
        key = os.path.relpath(str(r.path.parent), common)
        n_ok, n_bad = groups.get(key, (0, 0))
        groups[key] = (n_ok + (0 if r.errors else 1), n_bad + (1 if r.errors else 0))
    print("\n==== 汇总 (按 evidence_db 目录) ====")
    for key in sorted(groups):
        n_ok, n_bad = groups[key]
        print("%-55s %d 文件: %d 通过, %d 失败" % (key, n_ok + n_bad, n_ok, n_bad))
    n_err = sum(len(r.errors) for r in results)
    n_fail = sum(1 for r in results if r.errors)
    print("\n总计 %d 文件: %d 失败 (ERROR %d 个)" % (len(results), n_fail, n_err))
    if n_fail:
        print("门禁 B 判定: FAIL — 须修复 evidence_db 后重检")
        return 1
    print("门禁 B 判定: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
