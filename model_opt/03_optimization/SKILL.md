---
name: npu-optimization-implementation
description: 优化实施：用去重/复用/掩盖/替换四维度框架实施性能优化，含内存工程（峰值显存优化：分块计算、缓存生命周期、副本消除）。当用户需要实施优化方案、融合算子、预分配 buffer、编译工具（TorchScript/torch.compile）、flat forward、换等价实现、或降低峰值显存/解决 OOM 时触发。
---

# NPU 优化实施

## 进入条件（★A 文件校验，先于一切实施动作）

1. `validate_analysis.py --full analysis/round_{N}` **退出码 0**，校验器输出原文（含退出码）记入本阶段工作记录——**禁止以"自查/自认为完整"替代校验器运行**
2. `solutions.md` 覆盖 candidates.md §1 全部候选 id（校验器机检），无方案项附解释
3. **GATE 未过时禁止修改任何模型/运行代码**——发现产物缺失应回到 Phase 2 补做，而不是边补边实施

## 运行前统一原则：NPU 资源检查

**每次需要运行代码（benchmark / profiling / 精度验证 / 功能测试等）之前，必须先用 `npu-smi info` 检查 NPU 上是否有与本任务无关的进程。若存在，先确认这些进程与当前任务无关（必要时向用户确认归属），再清理进程、释放显存，确认资源干净后才开始运行。**

```bash
npu-smi info        # 检查各卡上的进程占用
ps -fp <PID>        # 确认进程身份与归属
kill <PID>          # 确认无关后清理（顽固进程用 kill -9）
npu-smi info        # 复查确认显存/算力已释放
```

## 定位

本阶段承接瓶颈分析（02）的 candidates.md（问题点清单），将每个问题点转化为具体的优化方案并实施。

核心流程：对照问题点设计方案（无方案的须解释）→ ★A 用户确认 → 逐条实施 → 验证精度和性能 → 用户确认后提交。

## 方案设计（对照问题点）

对照 `analysis/round_{N}/candidates.md` §1 的每个问题点，用四维度框架设计具体方案：

- **完备性**：每个问题点必须有对应方案，或明确解释为什么没有（不可行 / 收益不足 / 依赖前置改动等）——不允许静默遗漏
- 方案清单落盘 `analysis/round_{N}/solutions.md`：问题点 id | 方案（手段 + 实施要点）| 预期收益（对照反事实收益上限）| 风险等级 | 实施顺序
- 完整后进入 **★A 用户确认**（门禁见 [execution_protocol.md](../references/execution_protocol.md) 确认节点 A），仅实施用户确认的方案
- 实施方式见下方「逐条实施：subagent 深挖」；放弃任何方案须满足下方「Level 分级」

## 逐条实施：subagent 深挖

环境支持子 agent 时，**实施位置按方案级别路由**（不支持时主线逐条串行，纪律相同）。主 SKILL「探索规模路由」判轻量档时本节路由不生效——全部方案主线实施，验证纪律不变：

- **Level 1 微调级：主线直接实施**。spawn 开销（prompt 构造 + 冷启动 + 汇总）常超过任务本身，小改动不污染主线上下文；验证纪律不变
- **Level 2+：spawn 独立 subagent 实施**。探索过程脏（多轮尝试 / 失败归因 / 大量源码阅读），隔离价值大于 spawn 开销

### spawn prompt 三要素

subagent 零上下文启动，prompt 必须注入：

1. **任务与返回条件**：方案内容、对应问题点、验证要求（Level 1 精度 + A/B benchmark）。返回条件只有三种——已实施（附验证结果）/ 已放弃（附「方向放弃标准（分级）」对应级别完整证据）/ 已回退（附原因），不允许浅尝辄止：框架实现失败须再尝试自定义实现（Level 2+ 强制），单次失败须先归因再决定下一步
2. **必读文件**：按方案类型给路径、要求实施前先读——替换维度 → [npu_operator_catalog.yaml](references/npu_operator_catalog.yaml)；等价替换类方案 → [equivalence_verification.md](references/equivalence_verification.md)；图编译 → [compilation_tools.md](references/compilation_tools.md)；任何方案 → [npu_checklist.md](references/npu_checklist.md)；四维度手段 → 对应维度 reference
3. **死路清单**：本 session 已确认失败的路径（各附失败原因与成立前提）。清单内路径禁止重试，唯一例外是任务前提已实质变化（图结构 / 环境变量 / dtype / 版本），重验须说明前提差异

方案内的多轮尝试（换实现 / 调参数 / 换 dtype / 换输入顺序）在 subagent 独立上下文中穷尽，不挤占主线。相互独立的方案并行 spawn，有依赖的（方案 B 依赖 A 的产物）按依赖顺序。

### 主线职责

- 汇总各 subagent 结果，更新 solutions.md 实施状态，统一进入 Phase 4 验证
- 维护 session 级死路清单：subagent 报告失败路径时即时追加（含前提），供后续 spawn 注入
- 知识回写：subagent 实测发现与 reference 矛盾或超出其覆盖时，回写对应 reference（算子事实 → npu_operator_catalog.yaml 的 constraints/pitfalls；编译事实 → compilation_tools.md）。只写 evidence_db 不回写 reference，下个项目仍会踩同一坑；回写条目带前提（版本 / dtype / 图结构）

## 优化四维度

所有性能优化手段本质上只做四件事：

| 维度 | 核心问题 | 典型现象 |
|------|---------|---------|
| **去重** | "这个工作是必要的吗？能和相邻工作合并吗？" | 同类算子调用次数异常多；存在可合并的独立调用 |
| **复用** | "这个结果/资源之后还会被需要吗？" | 相同尺寸 tensor 反复分配释放；同一计算结果被重复计算 |
| **掩盖** | "这段延迟能和其他工作并行吗？" | 通信和计算串行排列；计算流中有可填充的空泡 |
| **替换** | "同样的结果有没有硬件更便宜的等价写法？" | 某算子落 AI_CPU；一组拆解算子有官方融合算子；某 API 有 NPU 更友好的等价表达 |

前三者改变工作量/工作方式，第四者改变同一工作的物理执行路径——它们正交，可组合。

每个维度的详细原理、具体手段和代码模式见对应 reference (**必读**)：

| 维度 | Reference | 核心内容 |
|------|-----------|---------|
| 去重 | [eliminate_redundancy.md](references/eliminate_redundancy.md) | 合并调用、消除冗余、清理框架开销 |
| 复用 | [reuse_and_precompute.md](references/reuse_and_precompute.md) | 预计算缓存、预分配 buffer、原地操作 |
| 掩盖 | [hide_latency.md](references/hide_latency.md) | 通信-计算重叠、双 buffer 流水 |
| 替换 | [equivalent_substitution.md](references/equivalent_substitution.md) | NPU 融合算子、换等价 API、换算法 |

## 内存工程（让单卡放得下）

四维度框架是速度导向的；"降低单卡峰值显存"是另一条有独立验收的工作线，两个触发入口：

1. **07 并行切分的承重前提**：切分方案经病因对账（见 [07 analysis_workflow「第零步」](../07_parallel_splitting/references/analysis_workflow.md)）确认不覆盖 OOM 阶段时（如按独立分支切分、主干每卡冗余），"单卡装下"由该方案确认时立项，**在切分实施前完成**
2. **Phase 2/3 直接发现**：瓶颈分析定位到显存受限（swap / OOM / 被迫小 batch）

手段（数值等价性要求与替换维度同级，验证协议见 [equivalence_verification.md](references/equivalence_verification.md)）：

- **分块计算**（row/tile chunking）：[N,...,C] 算子沿可切维分块执行，峰值有界、代数不变。注意两点：① 逐算子核对语义——广播对齐方式（如 mask 对 batch 维的广播）、自配对距离类算子（行块得到 [B,B] 而非 [B,N]）是实测高频坑；② GEMM tiling 改变引入 1e-7(fp32)~1e-5 级形状噪声，须与原实现 A/B 对齐
- **生命周期管理**：跨阶段死缓存的显式清理（算完即 clear）、旧引用提前释放（residual 读后即断）、in-place 替代 out-of-place 副本
- **布局与副本消除**：permute+contiguous 的大副本改为分块写入目标布局；避免瞬时双份

验收：真实规模（或缩配锚点）跑通 + 与原实现的分层 tolerance 对齐 + 峰值实测（`torch.npu.max_memory_allocated()`）留档。

**收敛熔断**：OOM 逐点转移（修一处挪一处）累计 ≥3 轮 → 停止逐点修补，先复审分诊归因（回读全部 OOM traceback 建崩溃位置图谱，与病因清单比对——实际崩溃位置与归因不符 = 初始内存模型错误，先修模型再修内存），再重估峰值模型（分配器碎片 / 隐藏常驻 / 算子 workspace——把实测差值作为隐藏项计入），仍无 ≥10% 余量则回 07 重选切分方案（内存工程换不来余量时，病因必须回到切分层解决）。

## 场景专用 Reference

以下文件按场景条件加载，不强制读取（npu_checklist 除外）：

| Reference | 加载条件 | 核心内容 |
|-----------|---------|---------|
| [npu_checklist.md](references/npu_checklist.md) | 每轮优化开始前必读 | NPU 已知性能陷阱的 grep 扫描清单 |
| [npu_operator_catalog.yaml](references/npu_operator_catalog.yaml) | 替换维度层 1 时加载 | 融合算子目录（被 equivalent_substitution.md 引用） |
| [equivalence_verification.md](references/equivalence_verification.md) | 实施任何等价替换（换 API/融合算子/换算法）时 | 单步等价性验证协议：代表性输入构造、确定性条件、距离函数与阈值 |
| [compilation_tools.md](references/compilation_tools.md) | host-bound 或存在可融合的算子碎片化瓶颈时 | TorchScript/jit.trace/torch.compile(npu)/NPU JIT 的选择决策树、编译前置条件（eager 收敛判定）、兼容性排查、编译粒度决策 |

> 多卡切分方案设计不在本子技能（属 [07_parallel_splitting](../07_parallel_splitting/SKILL.md)，Phase 0 分诊触发的条件轨道）；已多卡场景的通信优化按四维度框架走——通信-计算重叠见 [hide_latency.md](references/hide_latency.md)（掩盖），瓶颈类型判定见 [profiling_to_action.md](../02_bottleneck_analysis/references/profiling_to_action.md) 归因层 #8。

## 通用原则

- 每次优化后重新 Profiling，确认瓶颈是否转移
- GPU 最优实践在 NPU 可能反效果，必须实测验证
- 保留原始实现供 fallback
- 权重修改须保持 checkpoint 可加载且数值等价（原则见主 SKILL「核心原则 · checkpoint 兼容」，操作细节见 [reuse_and_precompute.md](references/reuse_and_precompute.md)「Checkpoint 兼容性」）
- **四维度逻辑正交，NPU 上经内存分配与异步流水线耦合**：一个方向的失败不否定独立方向；改变操作数量/顺序的优化必须以 L0 端到端 benchmark 为准
- **深度优先于广度**：对每个优化方向，穷尽探索（多种实现、完整验证）比浅尝多个方向更有价值——落地方式见「逐条实施」与「Level 分级」

## Level 分级：探索深度与放弃标准

失败必留证据的纪律不变，变的是试什么、试多深——**错误结论的传播成本 > 验证成本时才深挖**：架构级决策的错误会进 evidence_db 传染后续，必须穷尽；微调级失败不传播，一句话证据足够。

### 前置过滤（尝试任何路径前依次过三关）

1. **约束检查**：与硬约束冲突的路径跳过——环境强制项、用户决策项（如精度决策）、evidence_db 中前提仍成立的死路条目。例外：实验目的就是测该约束的边界
2. **便宜探针优先**：读源码 / `help()` / 单次 smoke 能判定的，不跑完整实验（编译 + benchmark 常是分钟级）；相关知识先查 reference（算子 → npu_operator_catalog.yaml，编译 → compilation_tools.md）
3. **死路清单查重**：见「逐条实施」spawn 三要素——清单内路径除非前提变化不重试

### Level 总表

| Level | 范围 | 探索深度上限 | 放弃须满足 |
|-------|------|-------------|-----------|
| 1 微调级 | 改参数 / flag / 跳过单步 / 1-3 行改动 | 1 次尝试 | A/B 对比（≥3 reps 中位数）+ 一句话原因（如"流水线耦合回退 +0.8ms"） |
| 2 替换级 | 换等价实现 / 融合算子 / 改数据流路径 | 框架路径 + 备选各 1 次 | 等价性验证（协议见 [equivalence_verification.md](references/equivalence_verification.md)）+ A/B + 失败归因（概念错误 / 框架 overhead / 硬件不友好 / 流水线耦合）；仅有框架实现且失败时，须再尝试 1 种自定义实现 |
| 3 架构级 | 重写算子 / 改变计算图结构 / checkpoint 重映射 | 穷尽；超上限须向主线说明新依据，主线批准才升级 | ≥2 种实现（1 框架 + 1 自定义/bare）各过微基准 → 小样本 → 全量三步并记录数值 + 归因 + 调参重试记录（换 dtype / shape / 输入顺序）；归因为"框架 overhead"时自定义实现为强制 |

**通用禁则**（所有级别）："我觉得不会通过"（未实测）、"diff 看起来有点大"（未量化）不是合理放弃原因。

### 验证成本控制

- **rigor 按赌注**：不可逆决策（默认路径切换 / 结构性变更 / 将写入 evidence_db 的结论）用 h2h 多组交替（如 3×30 reps）；<5% 量级、开关可回退的差异用 2×30 reps 中位数
- **预热税**：编译类优化落地后每次进程冷启动重付模型加载 + 编译/预热（全 shape 可达分钟级）——验证脚本按当前 regime 预热而非全 shape；探索编译产物跨进程缓存；加载 + 预热占单次验证 > 50% 时先消除该开销再跑验证循环
