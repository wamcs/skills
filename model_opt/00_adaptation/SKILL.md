---
name: npu-adaptation
description: NPU 模型适配（model_opt Phase 0）：环境检测与隔离 → 权重获取与格式管理 → 严格按原始文档实现推理脚本 → 冒烟测试与 OOM 分诊（决定是否多卡切分）→ 与迁移前 golden 值精度对齐。当用户需要把新模型跑通在 NPU 上、下载和管理模型权重、编写推理脚本、验证适配后精度、或排查推理 OOM 时触发。
---

# NPU 模型适配（Phase 0）

## 运行前统一原则：NPU 资源检查

**每次需要运行代码（benchmark / profiling / 精度验证 / 功能测试等）之前，必须先用 `npu-smi info` 检查 NPU 上是否有与本任务无关的进程。若存在，先确认这些进程与当前任务无关（必要时向用户确认归属），再清理进程、释放显存，确认资源干净后才开始运行。** 无关进程会争抢算力/显存，导致性能数据失真或 OOM。

```bash
npu-smi info        # 检查各卡上的进程占用
ps -fp <PID>        # 确认进程身份与归属
kill <PID>          # 确认无关后清理（顽固进程用 kill -9）
npu-smi info        # 复查确认显存/算力已释放
```

## 核心原则

- **原始文档为准**：推理配置必须来自模型 config.json / README / paper，不凭经验猜测
- **精度可验证**：完成标准不是"能跑"，而是"输出与原始实现等价"
- **环境可复现**：隔离、锁版本、一键激活
- **最小改动**：仅 `"cuda" → "npu"`，不改模型逻辑
- **适配不做优化**：融合注意力、混合精度、量化等显存/性能手段属于 Phase 2+ 四维度优化；适配阶段只做容量判定与正确性对齐

## 工作流

```
0.1 环境准备与项目初始化 → 0.2 权重获取 → 0.3 推理实现（含代码理解）→ 0.4 冒烟测试与 OOM 分诊 → 0.5 精度验证
```

### 0.1 环境准备与项目初始化

1. **加载环境参考**（一次性）：硬件诊断命令、torch_npu 功能验证代码、环境变量配置清单（基础/性能/Python 级）、版本配套确认方法、常见报错及解决方案，统一见 [environment_reference.md](references/environment_reference.md)
2. 向用户确认隔离方案 (Docker / venv / 系统直装)，安装依赖并锁版本
3. 生成一键环境启动脚本 `set_env.sh`：CANN 工具链激活 + 所选隔离方案的激活（venv → activate venv；Docker → 容器启动封装；系统直装 → 仅 CANN 激活）
4. 按标准结构初始化项目目录 + git init + `.gitignore`——结构定义与提交范围见 [standardized_operations.md](../references/standardized_operations.md)「标准项目目录结构」

### 0.2 模型权重获取

如果git clone的库中没有权重的话，使用 [scripts/weight_manager.py](scripts/weight_manager.py)：

```bash
# 探测当前网络能访问哪个源 (ModelScope > hf-mirror > HuggingFace)
python scripts/weight_manager.py detect-source

# 下载权重到标准目录。自动优选 safetensors 格式；若模型只提供 .bin 则 fallback 使用原格式，不做转换
python scripts/weight_manager.py download --model-id {id} --local-dir weights/

# 如果下载源同时提供了 safetensors 和 bin (冗余)，删除 bin 节省空间
python scripts/weight_manager.py cleanup --weights-dir weights/

# 加载权重验证完整性：确认无 missing keys、文件未损坏
python scripts/weight_manager.py verify --weights-dir weights/
```

### 0.3 推理实现

**代码理解**（写脚本前必做）：从全局到局部——包入口（`__init__.py`）→ 模型类定义（含 forward）→ 推理入口（main/CLI/pipeline）。关注 config.json 中的架构参数（hidden_size, num_layers, num_heads 等）。非文本输入（图像、蛋白质序列、音频等）需确认预处理路径，检查 `collate_fn` / `DataCollator` 是否有设备绑定逻辑。

**配置溯源**：按优先级从模型自带文件中提取每个参数——

```
config.json / preprocessor_config.json  (最权威)
  → README / Model Card
    → 原始论文
      → 官方示例代码
```

优先使用官方封装 (`AutoImageProcessor`, `AutoTokenizer`, `Pipeline`)。手动实现时逐项交叉验证。

特别注意适配过程中要和原版实现保持一致，禁止随意改动模型结构、参数、推理逻辑、backend参数等。

**设备适配写法**（最小改动原则的落地）：

1. **import 顺序**：先设环境变量（完整清单见 [environment_reference.md](references/environment_reference.md) §3），再 `import torch_npu`
2. **设备指定**：`torch.device("npu")`（或 `"npu:0"`）；可见卡由 `ASCEND_RT_VISIBLE_DEVICES` 控制
3. **CUDA 代码迁移**，两种方式选一：
   - 零改动：`import torch_npu` 后调用 `transfer_to_npu()`，`.cuda()` / `"cuda"` 自动映射到 NPU——适用于不改业务代码的场景（Phase 1 采集规范即基于此）
   - 手动替换：device 字符串 `"cuda"` → `"npu"`、`.cuda()` → `.npu()`、`torch.cuda.*` → `torch.npu.*`，逐处确认，禁止盲替换后不检查
4. **模型加载显式传 dtype**：`from_pretrained(..., dtype=...)`——不带 dtype 默认按 fp32 加载
5. 首次运行新 shape 触发在线编译、耗时长属正常现象，第二次即恢复

**禁止**：不查文档凭经验设参数、随意减步数、忽略 dtype。

### 0.4 冒烟测试与 OOM 分诊

**冒烟**（所有模型必做）：输出非 None、无 nan/inf、shape 正确。冒烟的目的是验证正确性，不是压满显存。

**OOM 分诊**（冒烟 OOM 后自动进入，无需用户指令）——先归因取证（Step 0），再走三条可判定的判据：

**Step 0 归因取证（先于一切判据）**：读到 OOM 后的第一动作是**定位**，不是估算。回读完整日志（不是 tail 窗口）：Python traceback 帧（模块 → 函数 → 行号）、报错算子名（`EL0004`/`EZ1001` 等 PID 行、`aclnnInplaceCopy` 等），连同进度条上下文（如 `Temp Embed 0/4`）记入归因记录。三条反偏误纪律：

- **静态估算不得替代位置读取**：Step 2 的估算用于解释"为什么这里会超"，位置必须来自 traceback。先有预测再有 OOM 时尤其如此——预测常被**类别级匹配**伪装成验证通过（"也是 XX 算子 OOM"，但同算子在不同模块/不同叠加下的内存模型完全不同）
- **"如预期"不免检**：预期内错误与意外错误的取证纪律同等强制——确认强先验比审查意外更需要做，因为它会固化错误模型并污染下游决策
- **归因是假设不是事实**：后续出现位置不符的新 OOM，必须复审既有归因（回读历史 OOM 日志建崩溃位置图谱比对），而不是把新位置当作独立的新问题

NPU 异步报错可能把错误同步点报在后续算子上——报错位置与调用栈明显不符时，用 `ASCEND_LAUNCH_BLOCKING=1` 重跑定位真实算子。

**Step 1 确认真实 workload**：向用户确认生产环境的序列长度 / batch 形态。回答两件事：冒烟输入是否失真（冒烟用了远超真实需求的 shape 触发 OOM、按真实 workload 估算放得下 → 换真实 workload 重跑冒烟即可）；估算的输入是什么。**用户想跑长序列/大 batch 不是"实现错误"**，它是容量问题的正当输入，直接进 Step 2。

**Step 2 静态显存预算估算**（按实际加载的 dtype 数参数字节 + 主导张量/激活峰值，以真实 workload 为输入，纯代码分析 + 算术，分钟级）：

- **权重常驻本身超限**（> HBM × 0.8）→ 直接进入 [07_parallel_splitting](../07_parallel_splitting/SKILL.md)，不进入实测
- 估算明显放得下 → OOM 另有原因，进 Step 3
- dtype 不一致在此自然暴露：估算按实际加载的 dtype 计数，原始 bf16 被默认加载成 fp32 时参数字节翻倍（`from_pretrained` 不带 `torch_dtype` 的坑）
- 识别主导张量时**逐个产出初步 OOM 根因组件清单**：组件名称、大小、沿哪维增长、随 workload 的缩放规律——既是"可修复 vs 需并行"的裁决依据，也随分诊结论带入 07 第一步（在其基础上补全全组件时间线与层内峰值分解，不重复测算）

**Step 3 小 shape 实测：归因估算偏差**（条件步骤，仅"静态估算放得下却 OOM"或估算接近限值时进入）：用小输入跑通并记录 `memory_allocated` 随 shape 的增长曲线——普通运行 + 显存统计，不是 profiler。跑通是前提：驻留、碎片只有跑起来才可见。

- **首要任务是归因静态看不见的运行时效应**：实测远超估算 → 按方向检查 `no_grad` 缺失 / 全局引用导致的张量驻留 / 分配器碎片（`max_memory_allocated` vs `memory_allocated`）——修复后回冒烟；驻留与碎片不修就切分，照样吃掉切分收益
- **估算接近限值时才外推**：曲线中常驻部分（权重）是基线偏移，可变部分才是外推对象；按 Step 2 记录的缩放规律拟合（线性 / 平方，不默认线性）外推到真实 workload 裁决。判放不下 → 进 07，增长曲线随根因清单一并作为 07 第一步输入
- 加载阶段即 OOM（尚未 forward）：显存与 shape 无关，本步不适用——按加载路径问题处理（加载期 dtype 中转或临时拷贝），复查无果即切分

**分诊结论**：

- **可修复**（输入失真 / 加载问题 / 驻留）→ 修正后回冒烟
- **本质需要并行** → 携带 Step 2 初步根因组件清单（及 Step 3 增长曲线，若跑过）与**崩溃位置（Step 0 取证的 traceback 帧与算子名）**进入 [07_parallel_splitting](../07_parallel_splitting/SKILL.md) 全流程（分析 → 实施 → 验证）——07 的「第零步病因对账」以崩溃位置校验根因清单，禁止只治速度不治容量；切分验证通过后回归 0.5 完成适配精度验证（天然在多卡配置下执行）

> 分诊止损：三条判据无果即切分，不在适配阶段死磕。简单数据并行（DP only，多独立样本）不属于模型并行，直接配置 `ASCEND_RT_VISIBLE_DEVICES` 多卡即可，不触发本流程。

### 0.5 精度验证

本阶段对齐**迁移前**：golden 值来自 GPU（优先）或无 GPU 时 CPU 上运行原始实现的输出——后续优化阶段的回归只对齐优化前 NPU 自身输出，不再依赖外部 golden。

两步走：**Golden 采集** → **按输出性质选择验证策略**。

**Golden 采集**（显式步骤）：在 GPU（优先）或无 GPU 时 CPU 上运行**原始实现**，保存输出供比对。若要在CPU上运行，要注意取一个合适的样本，保证CPU在合理的负载下运行。记录采集环境（设备、dtype、框架版本）保证可复现；落盘要求：离线可加载、不依赖采集设备：

```python
import numpy as np, json
np.save(f"golden/{sample_id}_output.npy", output.detach().float().cpu().numpy())
with open(f"golden/{sample_id}_meta.json", "w") as f:
    json.dump({"device": "cuda:0", "dtype": "float16", "torch": "2.5.1"}, f)
```

**按输出性质选择验证策略**：

| 输出性质 | 典型模型 | 验证策略 |
|---------|---------|---------|
| 确定性 + 可枚举 | 分类、匹配、QA | 构造有标准答案的输入，精确断言 (assert top1=="cat") |
| 确定性 + 连续值 | 特征提取、嵌入 | 跳过语义检查，直接做数值对齐 (golden vs NPU cosine/max_abs) |
| 随机性 + 可感知 | 图像/视频生成 | 合法性检查 (分辨率/像素范围/方差) + 感知指标 (CLIP-score 对 prompt 的匹配度) |
| 随机性 + 领域约束 | 蛋白质设计、分子模拟 | 领域合法性约束 (合法残基/能量有限/守恒律) + 统计基线 (论文报告的恢复率/RMSD 范围) |

数值对齐工具（适用于确定性+连续值策略）→ [scripts/compare_baseline.py](scripts/compare_baseline.py)（对比在 CPU 上进行，golden 与采集设备无关）

其余的暂时先自行判断

如果失败，回退到 0.3 推理实现，检查实现的问题

**决策逻辑**：先判断模型输出属于哪一类，再选对应策略。不要对所有模型套同一套验证方法。

验证通过后进入 [01_preparation](../01_preparation/SKILL.md) 采集基线
