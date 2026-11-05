#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""02 阶段分析产物落盘校验 (bottleneck_analysis)。

校验 skills 已声明的落盘产物是否存在、内容是否合法、信息是否覆盖
(见 references/merge_analysis.md「candidates.md 结构」、references/execution_protocol.md):
  analysis/round_{N}/
    - line_a_findings.yaml   Line A 源文件 (YAML, 顶层 meta/facts/suspects;
                             facts 非空, suspects 可为空——无可优化点时)
    - line_a_report.md       Line A 渲染报告 (非空, 含 "Line A" 标题)
    - line_b_report.md       Line B 报告 (非空, 含 "Line B" 标题与 "信号追踪表")
    - candidates.md          合并分析产物 (含 §1 候选清单 / §2 信号追踪表 / §3 排除与待定项)
    - solutions.md           Phase 3 方案清单, 仅 --full 校验
                             (candidates §1 候选 id 须全覆盖, 无方案须附解释;
                             零候选合法, §1 表格缺失打回)

校验原则: 只查存在性、合法性、覆盖度; 非规范一律打回 (文件名不合规视为缺失),
不做容错适配。

用法:
  python3 validate_analysis.py <工作目录 | analysis 目录 | round 目录>...
  python3 validate_analysis.py --full <工作目录>   # ★A 确认前 (含 solutions.md)
退出码: 0 = 通过, 1 = 存在不合规, 2 = 路径错误
"""

import argparse
import os
import re
import sys
from pathlib import Path

import yaml

ROUND_RE = re.compile(r"^round_.+$")


class RoundCheck:
    def __init__(self, path):
        self.path = Path(path)
        self.errors = []

    def err(self, msg):
        self.errors.append(msg)


def check_text_file(rc, name, markers):
    """markers: list of (marker, 说明); 缺失/为空/缺 marker 均为不合规。"""
    fp = rc.path / name
    if not fp.is_file():
        rc.err("缺少落盘文件 %s" % name)
        return
    try:
        text = fp.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        rc.err("%s 无法读取: %s" % (name, exc))
        return
    if not text.strip():
        rc.err("%s 内容为空" % name)
        return
    for marker, desc in markers:
        if marker not in text:
            rc.err("%s 缺少%s (%s)" % (name, desc, marker))


def check_findings(rc):
    fp = rc.path / "line_a_findings.yaml"
    if not fp.is_file():
        rc.err("缺少落盘文件 line_a_findings.yaml")
        return
    try:
        data = yaml.safe_load(fp.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        rc.err("line_a_findings.yaml 无法读取: %s" % exc)
        return
    except yaml.YAMLError as exc:
        rc.err("line_a_findings.yaml YAML 解析失败: %s" % str(exc).splitlines()[0])
        return
    if not isinstance(data, dict):
        rc.err("line_a_findings.yaml 顶层应为映射")
        return
    for key in ("meta", "facts", "suspects"):
        if key not in data:
            rc.err("line_a_findings.yaml 缺少顶层 '%s'" % key)
    if "facts" in data:
        facts = data["facts"]
        if not isinstance(facts, list) or not facts:
            rc.err("line_a_findings.yaml facts 应为非空列表 (三层事实必记)")
    if "suspects" in data:
        suspects = data["suspects"]
        if not isinstance(suspects, list):
            rc.err("line_a_findings.yaml suspects 应为列表 (无可优化点时可为空列表)")


def extract_candidate_ids(text):
    """从 candidates.md §1 候选清单表格提取候选 id (首列首个 token)。

    返回 (ids, table_seen): table_seen=False 表示 §1 下没有表格。
    """
    ids, table_seen, in_section = [], False, False
    for line in text.splitlines():
        if line.startswith("#"):
            in_section = "候选清单" in line
            continue
        if not (in_section and line.lstrip().startswith("|")):
            continue
        table_seen = True
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells or not cells[0] or set(cells[0]) <= set("-: ") or cells[0].lower() == "id":
            continue
        ids.append(cells[0].strip("*").split()[0])
    return ids, table_seen


def check_solutions(rc):
    fp = rc.path / "solutions.md"
    if not fp.is_file():
        rc.err("缺少落盘文件 solutions.md (Phase 3 方案清单)")
        return
    try:
        text = fp.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        rc.err("solutions.md 无法读取: %s" % exc)
        return
    if not text.strip():
        rc.err("solutions.md 内容为空")
        return
    cand_fp = rc.path / "candidates.md"
    if not cand_fp.is_file():
        return
    cand_ids, table_seen = extract_candidate_ids(cand_fp.read_text(encoding="utf-8"))
    if not table_seen:
        rc.err("candidates.md §1 无候选表格")
        return
    for cid in cand_ids:
        if cid not in text:
            rc.err("candidates.md §1 问题点 %s 在 solutions.md 无对应方案"
                   " (须附方案或无方案解释)" % cid)


def check_round(round_dir, full=False):
    rc = RoundCheck(round_dir)
    check_findings(rc)
    check_text_file(rc, "line_a_report.md", [("Line A", "Line A 标题")])
    check_text_file(rc, "line_b_report.md",
                    [("Line B", "Line B 标题"), ("信号追踪表", "信号追踪表")])
    check_text_file(rc, "candidates.md",
                    [("候选清单", "§1 候选清单"), ("信号追踪表", "§2 信号追踪表"),
                     ("排除与待定项", "§3 排除与待定项")])
    if full:
        check_solutions(rc)
    return rc


def discover_rounds(target):
    """返回 round_* 目录列表; 路径不存在返回 None; 无 round 返回 []。"""
    p = Path(target)
    if not p.exists() or p.is_file():
        return None
    if p.is_dir() and ROUND_RE.match(p.name):
        return [p]
    analysis = p if p.name == "analysis" else p / "analysis"
    if not analysis.is_dir():
        return []
    return sorted(x for x in analysis.iterdir()
                  if x.is_dir() and ROUND_RE.match(x.name))


def main(argv=None):
    ap = argparse.ArgumentParser(description="02 阶段落盘产物校验")
    ap.add_argument("targets", nargs="+", help="工作目录 / analysis 目录 / round 目录")
    ap.add_argument("--full", action="store_true",
                    help="加验 Phase 3 产物 solutions.md (★A 确认前使用)")
    args = ap.parse_args(argv)
    rounds = []
    for t in args.targets:
        found = discover_rounds(t)
        if found is None:
            print("路径不存在: %s" % t, file=sys.stderr)
            return 2
        rounds += found
    if not rounds:
        print("未发现任何 round_* 目录", file=sys.stderr)
        return 1
    results = [check_round(r, args.full) for r in rounds]
    common = os.path.commonpath([str(r.path) for r in results])
    for r in results:
        rel = os.path.relpath(str(r.path), common)
        if not r.errors:
            print("[OK]   %s" % rel)
        else:
            print("[FAIL] %s" % rel)
            for msg in r.errors:
                print("       ERROR %s" % msg)
    n_err = sum(len(r.errors) for r in results)
    n_fail = sum(1 for r in results if r.errors)
    print("\n总计 %d 个 round: %d 失败 (ERROR %d 个)" % (len(results), n_fail, n_err))
    if n_fail:
        print("02 落盘校验: FAIL — 补齐/修复后重检")
        return 1
    print("02 落盘校验: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
