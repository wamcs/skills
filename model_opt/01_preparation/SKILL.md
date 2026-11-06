---
name: npu-adaptation-preparation
description: Phase 1 基线准备：测试数据构造（性能 regime 代表 + 精度覆盖）、profiling 采集体系构建、精度回归脚本构建。当用户需要采集基线 profiling、构造测试数据、或构建优化前后精度对比脚本时触发。
---

# NPU 优化基线准备

> 模型跑通（环境搭建、权重获取、推理实现、OOM 分诊）属于 Phase 0 → [00_adaptation/SKILL.md](../00_adaptation/SKILL.md)。本子技能只负责：为优化阶段准备可复现的基线数据与验证脚本。

## 运行前统一原则：NPU 资源检查

**每次需要运行代码（benchmark / profiling / 精度验证 / 功能测试等）之前，必须先用 `npu-smi info` 检查 NPU 上是否有与本任务无关的进程。若存在，先确认这些进程与当前任务无关（必要时向用户确认归属），再清理进程、释放显存，确认资源干净后才开始运行。** 无关进程会争抢算力/显存，导致性能数据失真或 OOM。

```bash
npu-smi info        # 检查各卡上的进程占用
ps -fp <PID>        # 确认进程身份与归属
kill <PID>          # 确认无关后清理（顽固进程用 kill -9）
npu-smi info        # 复查确认显存/算力已释放
```

---

## 一、测试数据准备

【规则】**用户显式指定的测试数据场景优先**——用户在请求中给定的输入文件、shape/regime、batch/序列长度等，性能与精度数据均按指定场景构造/采集；本节判据（质变分档、中位代表、风险维度覆盖）仅是用户**未指定时**的默认选择。按指定场景得到的结论，表述限定在该场景内，不外推到未测 regime。

数据集服务两个目标，构造判据不同：

- **性能数据**（profiling 采集、wall-clock/L0 基线与收益比对）：核心判据是**场景之间是否带来不同的性能表现**。量小——每个性能 regime 一条代表，通常总共只有几条
- **精度数据**（优化回归验证，防精度跑偏；Phase 0 golden 采集同样适用）：核心判据是覆盖可能暴露正确性问题的维度

### 性能数据：按性能 regime 覆盖

识别会产生**质变性能表现**的场景，每个 regime 选 1 条代表输入：

| 性能维度 | 说明 | 判断 |
|---------|------|------|
| 输入规模档位 | 长度改变瓶颈结构时才分档（如 attention 占比随长度质变） | 同 regime 内 shape 差异只带来线性缩放，无需多采 |
| Batch 形态 | bs=1 vs bs>1、不同 padding 比例 | kernel 选择与访存模式可能不同 |
| 推理路径 | prefill vs decode、触发不同代码分支 | 不同分支 = 不同 kernel 序列 |

判据：两个场景的瓶颈画像（算子序列、L0 Computing/Free 结构）是否**质变**。质变才单独立 regime，线性缩放归入同一 regime。典型规模：2–5 条。

**统计稳定性靠重复测量，不靠样本量**：wall-clock 对同一条输入重复 ≥20 次取中位数（见 [profiling_collection.md](references/profiling_collection.md)），禁止用扩充样本数替代重复测量。

### 精度数据：按正确性风险覆盖

覆盖可能暴露正确性问题的维度，每个维度至少 1-2 条代表：边界条件（padding 边界、mask 形状、空/极短输入）、输入规模最短/中位/最长。

### L1 Profiling 样本

从性能数据中选生产主流 regime 的代表（生产 shape 分布的中位样本）1 条用于 L1 采集。L1 数据量大（可达数 GB）、分析重，同 regime 内无需多采。

每轮优化后验证收益时，若某 regime 的 L0 结构（L0 Free / L0 Computing 比例）与主流 regime 质变，或该 regime 的优化收益不符预期，再对该 regime 做 targeted L1。精度校验独立用精度数据集覆盖多维 shape/content。

---

## 二、Profiling 采集体系构建

采集级别（L0/L1）、三种性能测量及其覆盖范围、输出路径规范、采集前环境变量、完整代码模板与框架适配方案，统一见 [profiling_collection.md](references/profiling_collection.md)（唯一权威源）。

### 关键规则

- **不改业务代码的 CUDA 写法**：`import torch_npu` + `transfer_to_npu` 自动转换
- **多推理路径分离采集**：prefill/decode 等分别建立独立 profiling 段
- **GPU/NPU 对称采集**：跨平台对比时 GPU 用相同 schedule + `CUDA` activity

### 环境预检

采集前运行：
```bash
python <skill_path>/01_preparation/scripts/validate_profiling_env.py --device npu:x --output-dir ./profiling
```

---

## 三、精度回归脚本构建

**目标**：保存可信 baseline 输出，构建可一键运行的对比脚本。

**Baseline 语义**：本阶段保存的是**优化前 NPU 自身输出**，回答"优化有没有退化"；Phase 0 已完成与迁移前 golden（GPU/CPU 原始实现）的对齐，回答"NPU 初始实现是否正确"。优化阶段不依赖外部 golden。

### Baseline 来源与对比策略

基线来源即上文「Baseline 语义」确定的优化前 NPU 自身输出；对比方法论（按输出类型选择距离函数、阈值在比较前声明）遵循 [04_accuracy_assurance/SKILL.md](../04_accuracy_assurance/SKILL.md)。本阶段只构建脚本框架，不定义具体方法论。

### 工具

直接使用 [04_accuracy_assurance/scripts/compare_precision.py](../04_accuracy_assurance/scripts/compare_precision.py)（模板脚本，agent 填 5 个函数）：

- baseline 采集：`python compare_precision.py --mode baseline --runs 3`——输出落盘（离线可加载、不依赖设备、可复现），同时测自然波动 D_base
- 优化后对比：`python compare_precision.py --mode compare`——按输出类型选度量、按 3×P99(D_base) 校准阈值，输出判定报告（退出码非 0 = 未通过）

### 确定性条件

推理：`model.eval()` + `torch.no_grad()` + 固定输入 + 关闭随机性。详见 04_accuracy_assurance「一、前提条件」。

## 退出条件（缺一不得进入 Phase 2）

1. `validate_profiling_env.py` 退出码 0（环境预检）
2. `baseline/` 下 wall-clock json（≥20 次中位）与 L0 profiling 目录已落盘，`profiling/latest*` 软链有效
3. `compare_precision.py --mode baseline --runs 3` **已实际运行**且 `variance.json` 生成——只建脚本不采基线不算完成（精度门禁是后续每轮优化的闸门，闸门本体必须在 Phase 1 建立）
4. `evidence_db/metadata.yaml` 四节完整（`validate_evidence.py` 校验退出码 0），其中 `baselines.performance_ms` 已回填实际测量值
