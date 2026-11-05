# metadata.yaml 模板说明

## 定位

`metadata.yaml` 是 evidence_db 的**全轮共用元信息文件**,准备阶段(Phase 1)生成一次,所有轮次案例共享。

案例文件(`round_<N>.yaml`)不再记录 context——环境与基线信息以本文件为唯一来源,各轮不重复记录。

路径: `<workspace>/evidence_db/metadata.yaml`(与案例文件同目录)。

## 模板

```yaml
model:
  name: <string>            # 模型名称, 如 "Qwen3-1.7B" / "Z-Image-Turbo"
  domain: <string>         # 领域, 如 "LLM 文本生成 / 文生图 / 蛋白质结构 / 语音识别"
  architecture: <string>  # 架构类型, 如 "decoder-only transformer, GQA, 28 层"
  params: <string>          # 参数规模, 如 "1.7B" / "6.15B transformer + 4B text encoder"

test_data:
  characteristics: <string>  # 测试数据特征: 性能 regime 代表样本 + 精度覆盖样本的构造依据(见 01_preparation)
  input_spec: <string>      # 测试输入规格: batch / 分辨率 / 序列长度 / dtype / seed 等

baselines:                  # 基线怎么获取的,作收益与精度判定的锚点
  performance: <string>     # 性能 baseline 获取方式: benchmark 脚本、测量协议(次数/中位数/warmup)、计时口径
  performance_ms: <float>   # 性能基线数值(Phase 1 采集的端到端 wall-clock, 单位固定 ms)
                             # ——各轮链式衔接与累计加速比的锚点, 与 round_1 before 强校验相等
  accuracy: <string>        # 精度 baseline 获取方式: golden 来源(迁移前原始实现/CPU 参考实现等)、对比口径与容差

environment:
  hardware: <string>         # 如 "Ascend 910B2"
  software_stack: <string> # 如 "CANN 8.3.RC1 / torch_npu 2.6.0.post5 / torch 2.6.0 / diffusers 0.39.0"
```

## 填写说明

- 四节(`model` / `test_data` / `baselines` / `environment`)全部必填
- 测试数据构造依据见 01_preparation(性能 regime 代表 + 精度覆盖两套样本)
- 信息变化时(如升级 CANN、更换测试数据)更新本文件,并在受影响轮次案例的 `notes` 中注明变化与旧结论的失效
- 校验: `validate_evidence.py` 对 evidence_db 目录校验时会检查本文件的存在与字段完整性
