# metadata.yaml 编写指南

`metadata.yaml` 是 evidence_db 的**全轮共用元信息文件**,Phase 1 生成一次,所有轮次案例共享;案例文件不重复记录这些信息。回答四件事:优化什么模型(`model`)、测了哪些场景(`test_data`)、跟什么基线比(`baselines`)、在什么环境跑(`environment`)。

环境或测试数据变化时(升级 CANN、换卡、换数据)更新本文件,并在受影响轮次案例的 `notes` 里注明——旧结论可能失效。

## 模板

```yaml
model:
  name: <string>            # 含版本变体, 如 "openai/clip-vit-base-patch32" (不是裸 "clip")
  domain: <string>          # 任务领域, 如 "视觉-语言多模态 (zero-shot 分类 / 图文检索)"
  architecture: <string>    # 结构一句话: 层数/宽度/注意力形式/特殊结构
  params: <string>          # 参数规模

test_data:
  characteristics: <string>   # 构造依据: 为什么这些 regime 质变 (线性缩放不单独立档) + 精度样本覆盖哪些正确性维度
  regimes:                    # [v2] 性能场景清单——每个 regime 是一类真实工作负载
    - name: interactive_bs1   # 自解释命名, 被案例 final_state.regimes / performance_results 引用
      workload: <string>      # 模拟的真实场景 + 计时范围, 写到外人能看懂; 反例: "bs=1 推理"
      role: mainstream        # mainstream (生产主流, 恰好一个) | auxiliary
  input_spec: <string>        # 每种输入的形状/dtype/定长/seed + 各 regime 的 batch 形态

baselines:
  performance: <string>       # 完整测量协议: 脚本/计时范围/warmup/次数/统计量/隔离措施 (只写数字不可复现)
  performance_ms: <float>     # 主流 regime 基线 (ms)——round_1 链式锚点
  performance_regimes:        # [v2] 各 regime 基线, 辅 regime 不再散落 benchmark 输出
    - {name: interactive_bs1, baseline_ms: 36.95}
  accuracy:                   # 精度对齐方法 (推荐 mapping; 旧字符串仍接受)
    golden: <string>          # 参照输出来源: 谁在什么设备/什么 dtype 上产出
    method: <string>          # 对齐方法: 距离函数 + 阈值 + 判定规则 (只写 "PASS" 等于没写)
    coverage: <string>        # 覆盖的样本/维度, 未覆盖部分如实写
    result: <string>          # 对齐结论

environment:
  hardware: <string>          # 芯片/总卡数/实际使用的卡
  software_stack: <string>    # 全套版本: CANN / torch / torch_npu / Python——版本变化可能使旧结论失效
```

## 锚点关系(校验器强制)

| 锚 | 被锚对象 | 规则 |
|---|---|---|
| `baselines.performance_ms` | round_1 `end_to_end_before_ms` | 相等 |
| `performance_regimes[].baseline_ms` | round_1 `regimes[].before_ms`(同名) | [v2] 相等 |
| `performance_regimes` 主流行 | `performance_ms` | [v2] 相等 |
| `test_data.regimes[].name` | 案例中 regime 引用 | 名称一致 |

## 填写示例(真实项目摘录)

```yaml
model:
  name: openai/clip-vit-base-patch32 (ViT-B/32)
  domain: 视觉-语言多模态 (zero-shot 图像分类 / 图文检索)
  architecture: 双塔 CLIP — 视觉塔 ViT-B/32 (12层, width 768, patch 32) + 文本塔 Transformer (12层, width 512, ctx 77), 原始实现 github.com/openai/CLIP
  params: 151.3M

test_data:
  characteristics: >-
    regime 判据: 双塔全前向 / 单塔提取 / prompt 预计算 / bs=16 批量 四者瓶颈画像不同各立一档,
    bs 数值差异属线性缩放不单独立。精度覆盖: 真实图 bs=1 / 合成图 bs=8 (seed=42) / 极短文本 /
    77 token 截断 / 双塔相似度输出。
  regimes:
    - name: interactive_bs1
      workload: >-
        用户交互请求: 1 张图 + 5 个候选描述 ("这是猫的照片吗"), 返回匹配分数;
        计时 = 预处理 + 分词 + H2D + 双塔前向到 logits 的完整请求路径。
      role: mainstream
    - name: batch_bs16
      workload: 离线批量标注: 16 图 + 16 描述一次提交; 与 interactive 差异在 batch 形态的 kernel/访存模式。
      role: auxiliary
    - name: image_only_bs1
      workload: 图像入库提特征: 单图只过视觉塔 (文本特征已预计算, 不在计时内)。
      role: auxiliary
    - name: text_only_32
      workload: prompt 库预计算: 32 条描述只过文本塔, 服务启动时批量产出特征。
      role: auxiliary
  input_spec: >-
    图像 uint8 原图 → 3x224x224 fp32 (BICUBIC + CLIP 归一化); 文本 BPE int64 定长 77;
    合成图 seed=42; batch 形态: 1+5 / 16+16 / 1 / 32。

baselines:
  performance: >-
    各 regime 端到端 wall-clock (计时范围见 workload); benchmark.py 30 次中位, warmup 5;
    taskset -c 96-127 隔离核段; 物理卡 1。
  performance_ms: 36.95
  performance_regimes:
    - {name: interactive_bs1, baseline_ms: 36.95}
    - {name: batch_bs16, baseline_ms: 43.39}
    - {name: image_only_bs1, baseline_ms: 25.68}
    - {name: text_only_32, baseline_ms: 15.88}
  accuracy:
    golden: OpenAI 原始实现 + 官方权重在 CPU fp32 上运行产出 (Phase 0 采集)
    method: 特征 cosine >= 0.999 且 max_abs < 1e-2, logits js <= 1e-3 且 Top-1 一致; 优化回归用 NPU 自身 3 次重复 bit-exact 基线
    coverage: 5 条精度样本; 未覆盖 bs>8 的 batch 精度
    result: Phase 0 对齐 PASS; 三轮优化门禁 5/5 PASS

environment:
  hardware: Ascend 910B2 x 8 (64GB HBM/卡, 实际使用物理卡 1)
  software_stack: CANN 9.2.0 / torch 2.6.0+cpu / torch_npu 2.6.0.post5 / Python 3.12.12 (venv)
```

## 常见错误

- `workload` 只写 shape 不写场景("bs=1 推理")——外人看不懂,regime 白定义
- `performance` 只写数字不写协议——换环境后数字失效却无从判断
- 辅 regime 基线不记——收益对比失去锚点
- `accuracy` 只写"对齐通过"不写方法——无法判断可信度,也无法复现

## 校验

`validate_evidence.py` 检查本文件四节必填字段齐全非空;[v2] `regimes` 恰好一个 `mainstream`、`performance_regimes` 名称须落在 `regimes` 清单内、`accuracy` 为 mapping 时 `golden`/`method` 必填;并执行上表锚点校验。
