---
name: npu-accuracy-assurance
description: 精度保证（model_opt Phase 4，聚焦推理优化场景）：优化回归基线管理、分层验证、精度调试。当用户需要验证优化后精度、构建优化前后精度对比脚本、选择距离函数与阈值、或调试精度问题时触发。
---

# NPU 精度保证

精度验证的本质是**定义一个距离函数 d(baseline, optimized)，然后检查 d < 阈值**。框架与场景无关——训练对齐只是把比对对象换成 loss / 梯度 / 参数更新。

本文件按执行顺序组织：前提条件 → 确认基线 → 确定比什么 → 选择怎么比 → 执行验证 → 不通过时处理。

## 运行前统一原则：NPU 资源检查

**每次需要运行代码（benchmark / profiling / 精度验证 / 功能测试等）之前，必须先用 `npu-smi info` 检查 NPU 上是否有与本任务无关的进程。若存在，先确认这些进程与当前任务无关（必要时向用户确认归属），再清理进程、释放显存，确认资源干净后才开始运行。** 无关进程会争抢算力/显存，导致性能数据失真或 OOM。

```bash
npu-smi info        # 检查各卡上的进程占用
ps -fp <PID>        # 确认进程身份与归属
kill <PID>          # 确认无关后清理（顽固进程用 kill -9）
npu-smi info        # 复查确认显存/算力已释放
```

---

## 一、前提条件：确定性保证

精度对比的一切前提是输出可复现。不确定的输出无法做有意义的对比。

确定性要求：`model.eval()` + `torch.no_grad()` + 固定输入 + 关闭随机性；NPU 侧关闭可能引入差异的配置（如私有格式 `FLAGS_npu_storage_format=0`、确定性计算模式）。若结果不可复现，排查：随机性未关闭、JIT 编译引入不确定性、NPU 内部格式转换引入偏差。

**验证方法**：同一输入跑两次，确认输出可复现（bit-exact，或 diff 在自然波动范围内）。不通过则先修复确定性问题，再进入后续流程。

---

## 二、确认基线

精度对比的前提是有一个可信 baseline。来源不对，整个结论无效。

**基线语义（本子技能 = 优化回归）**：baseline 固定为**优化前 NPU 自身输出**，由 Phase 1 采集保存（见 [01_preparation/SKILL.md](../01_preparation/SKILL.md)「三、精度回归脚本构建」）。优化验证回答的是"优化有没有引入退化"，只要求优化前后对齐，**不依赖外部 golden**。与迁移前 golden（GPU/CPU 原始实现输出）的对齐属于 Phase 0 适配验证（见 [00_adaptation/SKILL.md](../00_adaptation/SKILL.md) 0.5），不在本阶段范围。

**切分场景（07 轨道）**：锚点配置（单卡可跑通）以切分前单卡输出为 baseline；生产规模（单卡放不下）以切分后、优化前的并行输出为 baseline——切分正确性由 `verify_split.py` 等价性验证背书，切分不改变模型数学语义。

**强制规则**：
- 始终与优化前的原始 baseline 对比，禁止与中间版本自比
- 先确认 baseline 可用再写对比脚本：文件齐全、输入可确定性重建、采集环境（设备/dtype/框架版本）与当前一致、采集时未混入优化 patch（检查方式见 [05_engineering/SKILL.md](../05_engineering/SKILL.md)「验证 baseline 有效性」）
- 样例级对齐 ≠ 模型级对齐（部分样本通过只能证明该范围内的对齐）

**自一致性验证**：优化前必须验证 baseline 自身确定性——用相同输入运行原始模型两次，确认输出一致。若 baseline 不确定，精度对比的阈值必须大于 baseline 自身波动。

---

## 三、确定比什么（验证边界）

"最终产物"有两层，选错层会导致验证结论无意义：

| 层次 | 说明 | 特点 |
|------|------|------|
| **模型直接输出** | forward 的返回值（logits、energy、图片、坐标等） | 始终可获取，快速 |
| **下游功能指标** | 用模型输出完成任务的评价（benchmark 分数、RMSD 等） | 对齐下游消费者真正关心的东西 |

**判断规则**：从模型直接输出到下游消费之间，是否有采样、随机过程、或非线性放大？
- **没有** → 验证边界 = 模型直接输出（直接输出等价即保证下游等价）
- **有** → 验证边界 = 下游功能指标（直接输出不等不代表功能退化）

### 常见模型类型速查

| 模型类型 | 直接输出 | 验证边界 |
|----------|----------|----------|
| LLM（贪心）/ CV 分类 / GNN 分类 / 力场 | logits / 物理量 | 直接输出 |
| LLM（采样） | logits | 下游功能指标 |
| CV 检测/分割 / 蛋白质结构 | bbox/mask/坐标 | 下游功能指标 |
| Diffusion | 像素矩阵 | 下游功能指标 |

度量按「四」的输出类型选择；边界情况与判断依据见 [model_family_hints.md](references/model_family_hints.md)。

---

## 四、选择怎么比（距离函数 + 阈值）

### 距离函数

按**五类最终输出**选择，且必须匹配下游消费者认为"等价"的语义（元数据字段：`output_type` 五类之一 + `output_semantics` 任务语义，以真实推理接口和任务 head 为准，不凭模型骨干推断）：

**1. Logits / 概率分布**（分类、NER、LM、ASR）：主指标 softmax 后 JS + Top-k 一致率；辅助 max_abs、P99 相对误差；序列逐位置对齐（teacher forcing）。cosine 有盲区——logits 整体平移时 cosine 变而概率不变，只作快速通道不作最终判据。

**2. Embedding / 表征向量**（CLIP、BGE、声纹）：主指标 cosine；辅助范数相对误差、Top-K 集合一致率（检索场景必须，低分差 query 微小漂移易翻序）。计算前按 attention_mask 过滤 padding。

**3. 连续张量 / 回归值**（深度图、频谱、物理量、时序）：MAE / RMSE / NRMSE，量纲明确时报带单位的绝对+相对误差；相关性可辅助不可替代；物理量禁只用 cosine。

**4. 结构化输出**（框、mask、关键点、3D、图）：匹配后比 IoU / mAP / PCK / RMSD / TM-score / Chamfer；后处理（NMS/置信度过滤/坐标变换）前后都要比。

**5. 生成输出**（token、图像、音频）：固定随机条件（seed/prompt/采样器/解码）下比张量——逐 token logits（贪心可加完全匹配率，采样下不可能 bit-exact）、SSIM/LPIPS、Mel 频谱；固定 seed 只是回归门禁，分布质量需多 seed。流式状态（KV cache）属接口状态，单独检查。

**比对的张量 = 消费语义层的最终输出**（按「三」判定边界：解码后的图像/音频/Mel 频谱），不是中间过程。多步迭代模型（Diffusion 等）的中间态（如去噪终态 latents）**不得作为门禁比对对象**——迭代会逐级放大 ulp 级数值差异，中间态阈值会变相要求 bit-exact，错误否决一切数值型优化；中间态比对仅作诊断（Level 3 定位）用途。

### 阈值

阈值不是固定的，由三个因素决定：

1. **dtype 上下文**：fp32 自然 diff ~1e-6；fp16/bf16 ~1e-3。阈值应高于自然 diff 但低于"功能错误"的 diff。
2. **自然波动基准（必测）**：在 baseline 模型上用相同输入运行 2-3 次，计算各次之间的距离函数值。**此步不可跳过**——不测就无法区分"优化引入的差异"和"硬件固有抖动"。
3. **验证范围**：单步等价用紧阈值；累积多步的端到端验证用松阈值。

**阈值校准公式**（把自然波动变成可推导的阈值）：`D_opt ≤ max(绝对误差下限, 3 × P99(D_base))`——D_base 为 baseline 重复运行的自然误差，D_opt 为优化版相对 baseline 的误差。

**按输出类型的起始阈值**（起点参考，最终以校准公式为准）：

| 输出类型 | 起始门槛 |
|---------|---------|
| 分类 logits | JS ≤ 1e-3，Top-1 一致 100% |
| 序列 logits | 平均 JS ≤ 1e-3，Top-1 ≥ 99.5% |
| Embedding | cosine ≥ 0.999 |
| 连续回归 | P99 相对误差 ≤ 1%（按业务容忍度调整） |
| 检测/分割 | 匹配 IoU ≥ 0.99，mAP/mIoU 变化 ≤ 0.1 个百分点 |
| 固定 seed 生成 | SSIM ≥ 0.99 / speaker cosine ≥ 0.99（仅回归门禁） |

**统一接受规则**：shape/layout 一致 + 无 NaN/Inf + max_abs ≤ 绝对阈值 + 相对误差 P99 ≤ 相对阈值 + 必要的离散一致性（Top-k / IoU / 排序）。统计报告 P99 与最大值，不只看均值。

**阈值必须在比较前声明，不可事后调整。**

---

## 五、执行验证

验证基于模板脚本 [scripts/compare_precision.py](scripts/compare_precision.py)：agent 填 5 个函数（`build_model` / `build_sample` / `run_inference` / `output_type`，structured 输出另加 `custom_metric`）——`--mode baseline --runs ≥2` 采集基线并测自然波动 D_base，`--mode compare` 按输出类型计算度量、按校准公式定阈值并输出 `precision_report.json`（退出码非 0 = 未通过）。

用户显式指定的测试数据/场景优先于默认样本选择（构造判据的优先级见 [01_preparation/SKILL.md](../01_preparation/SKILL.md)「一、测试数据准备」）；按指定场景验证的结论，表述限定在该场景内。

### 分级验证

**Level 1 快速验证**（每次修改后）：
- 样本：Phase 1 测试数据集子集（构造判据见 [01_preparation/SKILL.md](../01_preparation/SKILL.md)「一、测试数据准备」，覆盖正确性风险维度）
- 比较：模型直接输出，按输出类型选距离函数
- 目的：快速确认改动没有引入 bug

**Level 1.5 任务级降级验证**（Level 1 不通过时触发）：
- 触发条件：Level 1 不通过，**且**模型属于"输出到消费间有随机性"类型（LLM 采样、Diffusion 等）
- 做法：用**极小子集**的下游任务评测给出方向性信号
  - LLM：从论文 benchmark 中抽 10-20 题，确认分数在报告值合理范围内
  - Diffusion：对 5-10 个固定 prompt 生成图片，计算 LPIPS/FID
  - 蛋白质：对 3-5 个已知结构计算 TM-score
- 判定：任务级分数在官方报告值 ±2% 范围内则通过

**Level 2 全量验证**（每批提交前，提交门禁之一）：
- 样本：Phase 1 测试数据集全量（覆盖所有推理路径），报告 P99 与 worst case
- 比较：模型直接输出 + 下游功能指标（如适用）
- 有损优化（量化/剪枝/蒸馏）需更大样本深度与完整任务集——数据集不满足时先回补 baseline 采集（01），再验证
- **必须与 Profiling 确认收益一起作为提交前的两项门禁**

**Level 3 精度调试**（Level 1/2 不通过时）：
- 逐层对比定位退化起始位置（注册 hook 保存中间输出，逐层计算距离函数）
- 详见 [debugging_guide.md](references/debugging_guide.md)

---

## 六、验证边界（结论不得超出证据）

最终输出相似度通过**不能**证明：

- 中间层没有错误（后续层可能抵消中间误差）→ 定位靠 Level 3 逐层比对
- 未测试的 shape / 长度 / batch / 分支路径等价（部分 bug 只在多步累积或后续 regime 中暴露）→ 覆盖度按 Phase 1 数据集实际范围表述
- tokenizer / processor / 后处理 / NMS / 解码器未变化 → 外围组件单独验证
- 小数值漂移不会造成 Top-k / 阈值过滤 / 生成分支突变 → 敏感场景补离散一致性检查
- 随机过程在并发或不同调度下可复现 → 稳定性单独测试
- 优化后性能在所有 regime 都提升 → 性能结论归 Profiling，不混入精度结论

---

## 参考资料

| 文件 | 内容 | 加载时机 |
|------|------|----------|
| [scripts/compare_precision.py](scripts/compare_precision.py) | 精度对比模板：五类度量 + 自然波动校准 + 判定报告 | 执行 Level 1/2 验证时 |
| [model_family_hints.md](references/model_family_hints.md) | 各模型类型的验证边界判断与输出类型映射 | 遇到不在速查表中的模型类型时 |
| [debugging_guide.md](references/debugging_guide.md) | Level 3 精度调试的定位方法 | 验证不通过需要定位问题时 |
| [baseline_policy.md](references/baseline_policy.md) | baseline 完整性检查清单和报告措辞 | 使用 Phase 1 baseline 前或撰写结论时 |
