---
name: npu-model-optimization
description: NPU 模型适配与性能优化全流程（模型适配 → profiling 驱动的瓶颈分析 → 四维度优化 → 精度验证 → 工程化提交）。当用户需要把模型跑通/适配到 NPU、优化模型性能或推理/训练速度、分析 profiling 数据、定位性能瓶颈、做显存不足的多卡切分时触发。
---

# NPU 模型适配优化

## 运行前统一原则：NPU 资源检查

**每次需要运行代码（benchmark / profiling / 精度验证 / 功能测试等）之前，必须先用 `npu-smi info` 检查 NPU 上是否有与本任务无关的进程。若存在，先确认这些进程与当前任务无关（必要时向用户确认归属），再清理进程、释放显存，确认资源干净后才开始运行。** 无关进程会争抢算力/显存，导致性能数据失真或 OOM。

```bash
npu-smi info        # 检查各卡上的进程占用
ps -fp <PID>        # 确认进程身份与归属
kill <PID>          # 确认无关后清理（顽固进程用 kill -9）
npu-smi info        # 复查确认显存/算力已释放
```

## 启动协议

无论从哪个子技能进入，执行前必须：
0. 适配状态判定：找到既有推理脚本现场跑一遍——跑通则跳过 Phase 0 直接进入 Phase 1；跑不通或无脚本则进入 Phase 0（[00_adaptation](00_adaptation/SKILL.md)）
1. 确认当前在哪个 Phase（参见下方「全流程」）
2. 确认上一个 Phase 的产出已完成
3. 按全流程顺序执行，不跳步
4. 进入每个phase后，所有reference文件需要按需加载，确认已读/跳过状态，并在 evidence_db 中记录

## 核心原则

- **Profiling 驱动**：所有优化决策必须有 profiling 数据支撑
- **源码定位根因**：Profiling 只给出"哪里慢"，必须深入功能性源码的具体实现来定位"为什么慢"
- **精度优先**：每次改动后必须跑精度验证，未得到用户允许前，禁止对模型做量化操作
- **记录一切**：成功和失败方案都要记录
- **checkpoint 兼容**：旧 checkpoint 必须仍可加载且数值等价。默认保持 state_dict key/结构不变；当优化涉及结构性融合必须改变结构时，须提供与模型同处的确定性重映射函数，并通过等价性验证
- **禁止未仔细分析就动手**：禁止直接用"量化/图编译"来解决性能问题。要先分析瓶颈、定位根因、再定制优化方案。量化/图编译是直觉上的"万能钥匙"，但并不一定能解决问题
- **不要放弃**：无论是分析定位，还是优化实施，不要只想着简单尝试就结束，而是要穷尽探索（多种实现、完整验证）
- **每一轮优化重新分析**：每轮优化后，必须重新采集 profiling 数据、重新分析瓶颈、重新定位根因，禁止沿用上轮结论。

## 探索规模路由（Phase 1 末判定，★A 可调档）

subagent 深挖用于追求极致优化，不是默认形态——轻量模型纯主线实施效果相当，成本低一个量级。Phase 1 基线齐备后路由，用户可在 ★A 覆盖默认档。

判定信号（零额外成本可得）：wall-clock median、单 forward kernel 数、层同构度、evidence_db 有无同架构先例。

| 档位 | 默认触发 | 执行形态 |
|------|---------|---------|
| 轻量 | wall < ~10ms 且 kernel < ~300 且层高度同构（如 86M/12 同构层/228 kernel 的小 ViT） | 纯主线：两线分析主线串行，实施不 spawn，可行性靠 reference 查证 |
| 标准 | 中间地带 | 双线 spawn 分析 + Level 2+ 方案 spawn 实施 |
| 深挖 | 用户要求逼近下界，或首轮显示 >3× 空间且根因跨层交织 | 全量纪律 + 终局判断 |

档位只设默认，价值判断留给用户（高 QPS 小模型可声明升档）。轻量档纪律不降级：失败留证据、逐方案 Level 1 验证、Phase 4 门禁不省。

## 标准化操作规范

Profiling 采集、精度对比、标准项目目录结构等统一规范，确保一致性和可复现。详见 [references/standardized_operations.md](references/standardized_operations.md)。

## 全流程

> **术语：一个「优化阶段」= Phase 2 → Phase 4 的一轮迭代**

```
Phase 0  模型适配（00_adaptation；已适配模型经启动协议第 0 步跳过）
         ├─ 环境准备与项目初始化 → 权重获取 → 推理实现 → 冒烟测试
         ├─ 冒烟 OOM → 自动分诊（详见 00）→ 本质需要并行则进入 07 切分后回归
         └─ 与迁移前 golden（GPU/CPU 原始实现）精度对齐 → 适配完成
   ↓
Phase 1  基线准备
         ├─ 生成 evidence_db/metadata.yaml（模型/环境/基线元信息，模板见 06_evidence_db，全轮共用）
         └─ 采集 L0 基线 + wall-clock benchmark（全程仅一次，作为收益判定基准）
   ↓
┌─────────────────── 一个「优化阶段」（可迭代多轮）───────────────────┐
│ Phase 2  瓶颈分析                                                  │
│          ├─ Line B: Profiling 分析（采集 L1 → 脚本 → 定位可见瓶颈） │
│          └─ Line A: 源码分析（必做,三层事实+判据推导疑点,报告落盘）    │
│    ↓   合并分析 → candidates.md（问题点清单）                        │
│ Phase 3  优化实施                                                  │
│          ├─ 方案设计：对照每个问题点给方案或"无方案解释"（solutions.md）│
│  ★ A  用户确认方案（问题点→方案对照 → 确认/裁剪）                    │
│          └─ 逐条实施确认的方案（每条后 Level 1 快速精度验证）         │
│    ↓                                                              │
│ Phase 4  门禁验证                                                  │
│          ├─ ① 全量精度验证（Level 2）                              │
│          └─ ② 阶段末重新采集 L0，与基线/上一轮快速比对确认收益        │
└──────────────────────────────────────────────────────────────────┘
   ↓
 ★ B  用户确认提交（展示本批总结 + evidence_db 已记录 → 确认/回退）
   ↓
Phase 5  工程化提交（git commit + evidence_db 记录）
   ↓
 ★ C  用户确认是否继续（展示本轮总结 + 剩余瓶颈 → 继续/停止）
   ↓
 ├─ 继续 → 回到 Phase 2（必须：重新采集 L1 → 重新分析 → 重新估算优化空间，禁止沿用上轮结论）
 └─ 停止 → 结束
```

> **三种性能测量在流程中的落点**：
> - **wall-clock**：Phase 1 采一次作基线；每个优化阶段 Phase 4 再采一次做收益确认（无 profiler 开销，是收益判定的可靠依据）。
> - **L0**：Phase 1 采一次作基线；每个优化阶段 Phase 4 采一次做收益比对和下一轮 Phase 2 的 L0/L1 交叉验证。
> - **L1**：每个优化阶段的 Phase 2 开始前采集，交分析模块定位优化点（迭代回环时每轮都重新采）。
> 三者必须覆盖相同代码范围。定义与模板见 [profiling_collection.md](01_preparation/references/profiling_collection.md)。

## 执行协议（agent 程序约束）

三个用户确认节点控制迭代流程，若用户声明自动化执行（无需人工确认），门禁节点仍然存在，agent 需在内部做自动化判定。详细门禁规则必读，必须遵守 [execution_protocol.md](references/execution_protocol.md)：

- **★A 方案审核**（Phase 3 内：方案设计 → 实施之间）：Phase 2 产出问题点清单（candidates.md），Phase 3 对照**每个问题点**设计方案（无方案的须解释）并落盘 solutions.md（之后进行文件校验），经用户确认后逐条实施。前置：candidates.md 完整（信号追踪与疑点去向闭环）。
- **★B 提交审核**（Phase 4→5）：对照 ★A 确认的方案清单逐条交代实施结果（已实施/已放弃/已回退，附效果与原因），evidence_db 已记录且通过文件校验，才允许 git commit。
- **★C 继续确认**（Phase 5 后）：展示本轮总结 + 剩余瓶颈，询问是否开启下一轮。


## 子技能索引

| 阶段 | 子技能 | 触发时机 |
|------|--------|----------|
| Phase 0 | [00_adaptation/SKILL.md](00_adaptation/SKILL.md) | 模型适配：环境/权重/推理实现/冒烟 OOM 分诊/golden 精度对齐 |
| Phase 1 | [01_preparation/SKILL.md](01_preparation/SKILL.md) | 基线采集与脚本构建：测试数据、profiling 采集体系、精度回归脚本 |
| Phase 2 | [02_bottleneck_analysis/SKILL.md](02_bottleneck_analysis/SKILL.md) | 瓶颈分析:源码结构线 + Profiling 数据线 |
| Phase 3 | [03_optimization/SKILL.md](03_optimization/SKILL.md) | 实施具体优化手段 |
| Phase 4 | [04_accuracy_assurance/SKILL.md](04_accuracy_assurance/SKILL.md) | 验证精度、调试精度问题 |
| Phase 5 | [05_engineering/SKILL.md](05_engineering/SKILL.md) | Git 分支与提交纪律、性能数据校验、文档维护 |
| 案例库 | [06_evidence_db/schema.md](06_evidence_db/schema.md) | 优化案例的记录格式(schema 定义；案例数据存在项目工作目录 `evidence_db/` 下) |
| 并行切分 | [07_parallel_splitting/SKILL.md](07_parallel_splitting/SKILL.md) | Phase 0 冒烟分诊或用户报告显存不足/需要多卡时触发（条件轨道，非顺序阶段；与主流程的 handoff 见 00 与 07 各自的流程位置说明） |

## 各阶段要点

**Phase 0 模型适配**：让模型在 NPU 上正确跑通——环境隔离、权重获取、严格按原始文档的推理实现、冒烟 OOM 分诊（决定是否多卡切分）、与迁移前 golden 精度对齐。

**Phase 1 基线准备**：准备测试数据、构建 profiling 采集脚本和精度回归脚本（对齐优化前 NPU 输出；迁移前 golden 对齐已在 Phase 0 完成）。关键产出：可复现的**基线性能数据（L0 + wall-clock，全程仅一次，作为后续每轮收益判定的固定基准）** + 可一键运行的验证脚本。

**Phase 2 瓶颈分析**：新轮次强制重置——每轮重新采集 L1、重新分析，禁止沿用上轮结论（性能 profile 已变）。开局做下界分析路由重心（Free 侧 / Computing 侧），双线并行分析（Line A 源码线 + Line B profiling 线，各 spawn 一个 subagent）→ 合并产出 `candidates.md` 问题点清单。详见 [02_bottleneck_analysis/SKILL.md](02_bottleneck_analysis/SKILL.md)。

**Phase 3 优化实施**：两步走——**方案设计**：对照 candidates.md 的每个问题点，用四维度（去重、复用、掩盖、替换）框架设计具体方案（无方案的须解释原因），落盘 `analysis/round_{N}/solutions.md`，经 **★A 用户确认**；**逐条实施**确认的方案（实施位置路由与 spawn 注入三要素见 03「逐条实施」），每条改动后做 Level 1 快速精度验证，实施中放弃须满足 03 的「Level 分级：探索深度与放弃标准」。全部方案处理完毕即进入 Phase 4。

**Phase 4 精度验证 + Profiling 确认**：本批（本轮优化阶段）所有优化完成后，**必须依次完成**：
1. 全量精度验证（方法论见 [04_accuracy_assurance](04_accuracy_assurance/SKILL.md)）—— 与原始 baseline 对比，确认精度无退化
2. 重新采集 **wall-clock + L0**（按 Phase 1 相同方法）—— wall-clock 确认真实收益，L0 与基线/上一轮比对确认收益来源（L0 Computing 和 L0 Free 的变化）
3. 两项均通过后才可进入提交流程；任一不通过则回退或调整

**Phase 5 工程化提交**：全部工作在 optimize/ 分支进行，每批一个 commit，用户确认后合入 main。**必须先按 [06_evidence_db/schema.md](06_evidence_db/schema.md) 将本轮优化案例记录到项目工作目录的 `evidence_db/` 下（与 `profiling/` 同级），再执行 git commit**——案例未记录不允许提交（见 [execution_protocol.md](references/execution_protocol.md) 确认节点 B）。

## 迭代退出条件

由用户在确认节点 C 中决定是否继续。agent 应基于下界分析提供量化建议：**同时满足**「终局判断」三条件（端到端停滞 + 候选穷尽 + 量化等用户决策项已摆出，见 [bound_analysis.md](02_bottleneck_analysis/references/bound_analysis.md)）时才建议停止——Free 或 Computing 任一侧到下界只是瓶颈转移信号，转为路由下一轮重心，**不是停止理由**。

终局判断前必须穷尽 NPU 融合算子库，不能仅看 utilization 数字下结论。
