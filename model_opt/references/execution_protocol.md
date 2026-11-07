# 执行协议（agent 程序约束）

> 本文件是迭代流程中三个用户确认节点的详细门禁。方法论（分析思路 + 方案设计思路）在 [SKILL.md](../SKILL.md) 和 [profiling_to_action.md](../02_bottleneck_analysis/references/profiling_to_action.md)，本文件只管"执行时必须完成的程序约束"。

## 自动化模式门禁协议（三个确认节点通用的硬约束）

用户声明自动化（无人工干预）**只豁免"用户交互"，不豁免门禁本身**。

## 确认节点 A：方案审核（Phase 3 内：方案设计 → 实施之间）

Phase 2 给出瓶颈定位（candidates.md 问题点清单），Phase 3 将每个问题点转化为方案。方案设计完成后、逐条实施前，**必须**向用户展示方案清单并等待确认。

### 前置检查（展示前）

1. **Phase 2 产物完整**：在 `<工作目录>` 下执行 `python <skill>/02_bottleneck_analysis/scripts/validate_analysis.py --full .; echo "exit=$?"`——`exit=0` 才通过
2. **方案完备**：`analysis/round_{N}/solutions.md` 中，candidates.md §1 的**每个问题点**都有对应方案，或附"无方案解释"（不可行 / 收益不足 / 依赖前置改动等）——不允许静默遗漏（`validate_analysis.py --full` 已含候选 id 覆盖机检）

此处需由一个完全独立的subagent进行脚本执行检查校验，以其判断结果为准，若不通过需要需要修改文件格式或者补充产物。该subagent也要对方案进行一轮挑战质疑，如有问题，要求方案提供者完善方案再放行

### 展示与确认

向用户展示 solutions.md 方案清单：

1. 每条方案包含：对应问题点（问题 + 位置 + 结构根因）、方案内容、预期收益（对照该问题点的反事实收益上限）、风险等级
2. 无方案的问题点逐条展示解释，供用户判断是否要求重新设计
3. 使用 `ask_user_question` 询问用户：
   - 方案清单是否合适、有无遗漏
   - 哪些方案实施、优先级如何
   - 是否有额外想尝试的方向
4. 根据用户反馈调整方案清单——之后**逐条实施**确认的方案（每条实施后 Level 1 快速精度验证；实施中放弃须满足 [03_optimization/SKILL.md](../03_optimization/SKILL.md)「方向放弃标准（分级）」）

**如用户声明自动化（无人工干预），此处默认执行全部方案，不再向用户展示**

## 确认节点 B：提交前审核（Phase 4 → Phase 5 之间）

### 前置检查

确认 evidence_db 案例已按 [schema](../06_evidence_db/schema.md) 记录到项目工作目录的 `evidence_db/` 下。在 `<工作目录>` 下执行（`<skill>` 为本 skill 的 model_opt 目录）：

```bash
# 1. 案例校验（退出码必须为 0）
python <skill>/06_evidence_db/scripts/validate_evidence.py evidence_db; echo "exit=$?"
# 2. 生成/更新总结报告（summary.md + summary.json）
python <skill>/06_evidence_db/scripts/generate_summary.py evidence_db
```

任一命令退出码非 0 → 修复产物后重跑，禁止提交

此处需由一个完全独立的subagent进行两脚本执行检查，以其判断结果为准，若不通过需要需要修改文件格式或者补充产物。此外此agent需要对产生的yaml文件和summary文件进行简单review，确保语义的可理解性和列出数据直接计算关系的准确性，如有问题，也需要打回修改内容。

### 展示与确认

本批优化的精度验证和 profiling 确认均通过后、git commit 前，**必须**向用户展示本批总结并等待确认：

1. **对照 ★A 确认的方案清单逐条交代**：已实施（实际效果：性能数据 + 精度数据）/ 已放弃（附原因，须满足 [03_optimization/SKILL.md](../03_optimization/SKILL.md)「方向放弃标准（分级）」）/ 已回退（附原因）
2. 使用 `ask_user_question` 询问用户：
   - 是否确认提交本批优化
   - 是否需要回退某些改动
3. 用户确认后才执行 git commit

**如用户声明自动化（无人工干预），此处默认提交且无改动，不再向用户询问**

## 确认节点 C：继续优化确认（Phase 5 之后）

git commit 完成后，**必须**向用户展示本轮总结并询问是否继续：

1. 展示本轮优化总结（性能提升 + 精度状态）
2. 使用 `ask_user_question` 询问用户：是否继续下一轮优化？
3. 用户确认继续 → 回到 Phase 2 开启下一轮。新一轮优化需要重新仔细分析新的优化点，拒绝仅处理上一轮遗留点。
4. 用户确认停止 → 运行 `06_evidence_db/scripts/generate_summary.py <工作目录>/evidence_db` 生成总结报告——一次产出两个文件（`summary.md` 人读报告 + `summary.json` 机器可读完整镜像，同一数据源渲染、信息对齐），然后结束

**如用户声明自动化（无人工干预），进行下一轮优化，直到到达用户声明优化轮数或者连续两轮尝试无进一步优化空间**。进入下一轮前必须执行 [02_bottleneck_analysis/SKILL.md](../02_bottleneck_analysis/SKILL.md)「进入条件」与「轮次开局协议」