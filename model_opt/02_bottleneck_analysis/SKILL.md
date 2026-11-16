---
name: npu-bottleneck-analysis
description: 瓶颈分析：profiling 数据分析 + 源码结构分析双线定位性能瓶颈。当用户需要分析性能瓶颈、查看 profiling 数据、定位慢的根因、或分析源码优化机会时触发。
---

# NPU 瓶颈分析

## 进入条件（不满足则停下补做，禁止进入分析）

1. **round ≥ 2 时，上一轮产物已过校验**：`validate_analysis.py --full analysis/round_{N-1}` 退出码 0
2. **round ≥ 2 时，本轮 L1 必须新鲜**：本轮 profiling 目录的时间戳晚于 round_{N-1} 相关文件的最后一次 git commit（禁止消费上轮 profiling 数据下结论）

## 轮次开局协议（round ≥ 2 强制，防"沿用上轮结论"）

1. **重置采集**：L1 必须本轮重新采集（上轮 L0 仅作交叉验证参照）
2. **上轮候选处置表**：上轮 candidates.md §1 逐条标注 `复验通过` / `条件失效`（附新画像数据）/ `已实施`（附收益数据）——处置表放入本轮 candidates.md §3
3. **新信号要求**：本轮 §1 至少含 1 个上轮不存在的信号 id，但不得虚构优化点强行续轮
4. **遗留项表达规范**：上轮遗留候选只能写成**可证伪的重验条件**（"若新画像显示 X 仍 > Y ms，则…"）放入处置表，禁止写成排期（"round N+1 处理"）

## 运行前统一原则：NPU 资源检查

**每次需要运行代码（benchmark / profiling / 精度验证 / 功能测试等）之前，必须先用 `npu-smi info` 检查 NPU 上是否有与本任务无关的进程。若存在，先确认这些进程与当前任务无关（必要时向用户确认归属），再清理进程、释放显存，确认资源干净后才开始运行。** 无关进程会争抢算力/显存，导致性能数据失真或 OOM。

```bash
npu-smi info        # 检查各卡上的进程占用
ps -fp <PID>        # 确认进程身份与归属
kill <PID>          # 确认无关后清理（顽固进程用 kill -9）
npu-smi info        # 复查确认显存/算力已释放
```

## 双线分析模型

Phase 2 的分析由两条线驱动,完全解耦、可并行:

| | Line B: Profiling 分析 | Line A: 源码分析 |
|---|---|---|
| 起点 | profiling 数据中的异常 | 源码的计算结构 |
| 发现 | 可见瓶颈(算子慢/空闲/等待) | 结构性疑点(可合并/可复用/可预计算/可替换的机会) |
| 方法 | 脚本解析 + 根因追踪 | 三层事实记录 + 判据推导 |
| 数据源 | profiling CSV/JSON | 仅源码(不消费 profiling) |

**执行流程**:

1. 采集 L1
2. **双线并行分析**——环境支持子 agent 时，两条线各 spawn 一个 subagent 执行（环境不支持、或主 SKILL「探索规模路由」判轻量档时，主线串行执行，产出要求相同）：
   - **Line B subagent**：跑 `run_analysis.py --output analysis/round_{N}/line_b_report.md`，按 [profiling_to_action.md](references/profiling_to_action.md) 方法论对全部 DEFINITE/WARNING 信号做根因追踪，返回追踪记录
   - **Line A subagent**：穿透框架 → 按三层记录事实 → 对照判据推导疑点（详见 [proactive_source_analysis.md](references/proactive_source_analysis.md)），填写 findings.yaml 并跑 `line_a_report.py` 落盘报告
   - subagent 的独立上下文让每条线都能做深（Line B 逐算子下钻、Line A 通读源码）而不挤占主线；spawn 时须在 prompt 中给出对应方法论文档路径、L1 目录与产物要求
3. **合并分析**（主线执行，需要两份报告的完整信息，不宜下放）(结合两份报告做整合,非简单拼接):以位置/算子为键**关联**两线发现 → **合成**完整候选(结构根因 + 量化影响) → **去重**双来源发现 → **补盲**(Line B 信号无结构支撑走桥梁追踪或定向回补 Line A;Line A 疑点无现象判冷路径排除或标 profiling 盲区) → 产出 `analysis/round_{N}/candidates.md` → 进入 Phase 3 方案设计。结构与合并动作的**唯一权威定义**见 [merge_analysis.md](references/merge_analysis.md)

**关键约束**:
- Line B 和 Line A **都必须执行**,不设跳过条件——Line B 覆盖可见瓶颈,Line A 覆盖 profiling 盲区
- **round ≥ 2 时 Line A 增量收敛**：范围 = 本轮变更涉及的实现/图结构 + 上轮遗留疑点，不重做全量三层分析（首轮必须全量建立基线认知）；Line B 照常每轮全量重采重析。图/编译 dump 只保留摘要级算子清单，原始 dump 不入库
- 两线不互相消费:Line A 的分析范围由生成模式推导的热路径收敛(非热路径只记架构行);疑点量化发生在合并阶段
- 候选 = 结构根因(Line A) + 量化影响(Line B)的交点——散落的信息点只有交叉关联后才成为完整候选

## References 索引

| 文件 | 内容 | 何时加载 |
|------|------|---------|
| [bound_analysis.md](references/bound_analysis.md) | 下界分析：分解模型、空间路由、终局判断 | Phase 2 开局（估算两侧剩余空间） |
| [proactive_source_analysis.md](references/proactive_source_analysis.md) | Line A 方法论：穿透框架、findings.yaml 组织、疑点判据 | 执行 Line A 时（subagent prompt 附带） |
| [profiling_to_action.md](references/profiling_to_action.md) | Line B 方法论：现象→归因→源码定位 | 执行 Line B 时（subagent prompt 附带） |
| [profiling_scripts_guide.md](references/profiling_scripts_guide.md) | 各解析脚本的输出含义与用法 | 消费脚本输出、选择 `--filter` 时 |
| [merge_analysis.md](references/merge_analysis.md) | 合并动作、candidates.md 结构、候选评估 | 合并分析阶段 |


## Line A: 源码分析

**流程**:
1. 穿透框架层,定位真实的模型实现代码(跳过 generate/pipeline/Module.__call__ 等 wrapper)
2. 按三层记录事实:架构理解(组件/重复度/生成模式) → 实现逻辑(数据流/生命周期/控制流/访问模式) → 算法(计算块的数学表达与实现路径)。粒度=计算块/主导张量,范围由热路径收敛
3. 对每条事实问四类根问题(必要性/重复性/独立性/表达)推导疑点——命中判据表的按表取值,未命中的手工填字段——填写 findings.yaml
4. 运行 `line_a_report.py` 渲染报告,落盘到 `analysis/round_{N}/line_a_report.md`(非法输入拒绝渲染)

Line A 与 Line B 完全解耦:不消费 profiling 数据,疑点不做量化(只标定性影响),量化在合并阶段完成。

详见 [proactive_source_analysis.md](references/proactive_source_analysis.md)。

## Line B: Profiling 分析

**流程**:
1. 采集 L1 profiling（使用 Phase 1 选定的生产中位样本，见 [01_preparation/SKILL.md](../01_preparation/SKILL.md) 第一节）
2. **下界分析（前置步骤）**：在跑脚本之前，读取上一轮 L0 的 Computing/Free 占比，估算两侧剩余空间并路由本轮分析重心（Free 高 → host 侧/掩盖；Free 低 → device 侧 Computing 压缩链）。详见 [bound_analysis.md](references/bound_analysis.md)。

3. 运行 `run_analysis.py`（统一入口）提取结构化数据，输出两段式报告：**总章**（全局优化空间 + 信号清单 + 通信疑点 Top-K，按 [DEFINITE]/[SIGNAL]/[FUTURE] 分组）+ **细节章**（A~H 节完整 statistics，供下钻参考；多卡目录时 H 节自动含跨 Rank 对比）。总章自动包含 L0/L1 交叉验证（传入 `--l0-dir` 时对比 L0 和 L1 的 step_trace，未传入时标注"未经交叉验证，须谨慎"）。**报告默认落盘到 `analysis/round_{N}/line_b_report.md`**（`--round N` 指定轮次，`--output` 可覆盖路径）。各脚本输出含义详见 [profiling_scripts_guide.md](references/profiling_scripts_guide.md)
4. **推理与根因追踪（强制，覆盖所有显著发现，不可跳过）**：阅读完整报告后，按 [profiling_to_action.md](references/profiling_to_action.md) 的方法论（现象→归因→源码定位）从信号组合定位瓶颈类型，再通过三座桥（Call Stack、Input Shapes、下发时序）从 profiling 数据定位到**源码中的具体代码位置**，沿调用链追溯根因。定位到源码后回答：**这段代码为什么导致了这个 profiling 现象？**

   "显著"的判定标准 = 脚本自身输出的 DEFINITE 信号 / WARNING 警告，或占比超过脚本定义的阈值。

   **参考示例**（非穷举，仅为说明不同发现类型可能需要不同的追踪路径）：

   - 算子开销高（来自 op_statistic / operator_details）→ `--filter <op>` 获取 Call Stack → 追溯到调用该算子的 Python 函数 → 判断是必要计算还是框架内部操作
   - 设备空闲 / 流间隙（来自 step_trace / trace_view）→ trace_view 的 Host2Device Bound Regions + async_npu flow 回连 cpu_op Call Stack → 定位哪段 Python 代码导致设备等待
   - Host 开销分类中的"other"占比高（来自 operator_details）→ 该类别是未归类的 host 操作聚合 → 按 host self time 排序找到具体算子 → `--filter` 追 Call Stack
   - 内存高频抖动（来自 memory_record / operator_memory）→ 重复同尺寸分配列表 → 对应算子的 Call Stack → 定位哪个操作在反复分配/释放
   - AI_CPU 回退（来自 op_statistic Core Type 分布）→ `--filter <op>` 获取 Input Shapes → 判断 dtype/shape 是否不匹配导致 fallback
   - straggler 信号（多卡目录时来自 H 节 H7 跨 Rank 对比：某 rank 在多数高 wait 算子上 wait 最小 = 最后到达的被等者）→ 该 rank 计算侧慢：对其跑单卡脚本（A~H 节，`--rank N`）重复本流程定位瓶颈
   - 通信疑点（H 节疑点块 / 总章 §3，多卡目录时出现）→ `--trace-source <op>` 溯源到调用通信的源码位置（置信度 [ALIGNED]/[RISKY]/[AGGREGATED] 随行）→ 按归因层 #8 细分产出候选。通信判读规则见 [profiling_scripts_guide.md](references/profiling_scripts_guide.md) §parse_communication

   **执行规则**：
   - 每个脚本运行后，先记录该脚本产出的所有 DEFINITE/WARNING 信号
   - 对每个信号，选择能将其连接到源码的桥梁（或多桥梁组合），执行根因追踪
   - 追踪产出格式：`发现来源 | 发现内容 | 使用的桥梁 | 源码位置 | 根因 | 候选方案`
   - 如果追踪过程中发现了**之前未识别的优化机会**，必须加入候选清单

   **门禁规则**：
   - 所有脚本的 DEFINITE 信号和 WARNING 警告全部完成根因追踪后才能进入候选合并
   - 不得用"这个信号看起来不重要"跳过追踪——脚本的 DEFINITE/WARNING 标记是脚本自身定义的显著性判断，agent 不可覆盖
   - 追踪到的根因如果是"框架内部操作"，必须进一步追到"是框架的哪段代码导致的"

5. 确认根因后,根因追踪记录(含候选方向)封卷进入合并分析——与 Line A 疑点关联/合成/去重/补盲,量化(反事实收益上限)在合并阶段完成,评估方法见 [merge_analysis.md](references/merge_analysis.md) §候选评估。

如需对单个脚本做 `--filter` 深入查询（如 `parse_operator_details --filter Transpose` 获取 Call Stack），可单独调用对应脚本。

**门禁规则**：
- 报告中任何 **DEFINITE** 信号或 **WARNING 警告**（由脚本自身定义，如"严重 Host-Bound"、"AI_CPU Fallback"、"高内存 churn"）**必须**有对应的根因追踪记录（候选或附 profiling 数据依据的显式排除），落盘于 candidates.md §2 信号追踪表

## 下一步

两份报告（`analysis/round_{N}/` 下的 line_a_report.md 与 line_b_report.md）封卷后,执行**合并分析**（关联/合成/去重/补盲,产出 `candidates.md` 问题点清单,见上方「双线分析模型」执行流程第 3 步）,进入 Phase 3 方案设计（[03_optimization/SKILL.md](../03_optimization/SKILL.md)）。
