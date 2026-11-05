# 案例库 Schema 说明

## 目录

- [定位](#定位) / [当前阶段目标](#当前阶段目标)
- [Schema 字段说明](#schema-字段说明)——记录案例时按字段查：phenomenon / analysis_path / root_cause / optimization / final_state / costs；环境与基线元信息不在案例文件中，见 [metadata.md](metadata.md)（准备阶段生成的 metadata.yaml，全轮共用）
- [切分案例文件 splitting.yaml](#切分案例文件-splittingyaml)——多卡切分案例单独落盘，不占轮次命名
- [填写原则](#填写原则) / [目录结构](#目录结构)

## 定位

本文件是案例库的**构造语法说明**,agent 据此在项目工作目录的 `evidence_db/` 下记录优化案例。

> **路径约定**: 案例数据存在**项目工作目录** `<workspace>/evidence_db/` 下(与 `profiling/` 同级),**不在 skill 目录中**。目录含两类文件:全轮共用的 `metadata.yaml`(准备阶段生成一次)与各轮案例 `round_<N>.yaml`。skill 目录只存本 schema 定义。

## 当前阶段目标

现阶段案例库的唯一目的是**把优化过程中的信息尽可能完整地记录下来**。消费端(检索、匹配、统计)是后续的事——先确保"存对了",再谈"怎么用"。

## Schema 字段说明

```yaml
- id: <string>
  # 唯一标识符。格式: <模型名称>-<优化轮数>-<日期YYYYMMDD>
  # 例: "qwen3_7B-round_1-20260706", "CLIP-round_2-20260710"

  profiling_ref: <string, optional>
    # 本轮案例分析所基于的 profiling 数据路径或时间戳(一轮通常共用同一份)
    # 如 "profiling/L1_20260729_134016" 或 "profiling/latest_L1"

  phenomenon:
    # 只记录与案例相关的信号——即被 analysis_path[].from_signals 引用者
    # (完整信号清单留存在 02 阶段 analysis/round_{N} 产物中,此处不重复罗列)
    # 信号来源两类: profiling 脚本输出 + 源码结构提取(02 Line A),都要记
    signals:
      - id: <string>  # 信号唯一 id,如 "S1"(或沿用 02 的信号名如 "op_statistic:top3_concentration")
        source: <string>  # 产出此信号的来源: 脚本名+参数(如 "parse_op_statistic" / "parse_kernel_details --filter Transpose"),
                           # 或源码位置(如 "protein_mpnn_utils.py:1104-1188 sample() 自回归循环")
        content: <string>  # 输出/源码的原文摘录(关键数值+结构事实),不做解读,只记事实
    raw_context: <string, optional>
      # 补充任何脚本/源码没覆盖但 agent 观察到的信息
      # 如: trace_view 中肉眼看到的 pattern

  analysis_path:
    # 从现象到结论的完整推理过程,可能存在多条并行分析线,每条线一个条目
    # 每条线: 从若干信号出发(from_signals) -> 分析步骤(steps) -> 本线结论(conclusion)
    # 重点记录 agent 针对哪些信息做了深入分析定位、整条分析路径是什么
    - id: <string>  # 分析线唯一 id,如 "A1"(或沿用 02 的 LA/RC 编号如 "LA-003")
      from_signals: <list[string]>  # 出发的信号 id(phenomenon.signals[].id),一条或多条
      steps:
        - action: <string>   # agent 做了什么(跑了什么脚本/读了什么代码/做了什么推理)
          observation: <string>  # 得到了什么结果/看到了什么
          reasoning: <string, optional>  # 为什么做这一步/这个结果说明什么
      conclusion: <string>  # 本分析线的结论(定位到的问题/根因/排除项),须自含,不引用外部文件代号

  root_cause:
    # 各分析线 conclusion 的聚合总结: 归并各线结论(可引用分析线 id),给出最终根因判定
    description: <string>  # 最终确认的根因(归纳各分析线 conclusion)
    bottleneck_type: <enum, optional>
      # Host-Bound / Compute-Bound / Memory-Bound / Allocator-Bound / Execution-Mode
      # 不明确或属于多种: 写 "mixed" 并在 description 中说明
    evidence: <string>  # 支撑此根因判断的关键证据

  optimization:
    attempts:
      # 每个执行的优化点一条记录,禁止把多个优化点打包成一条
      # 未实施的前置排除项不进 attempts——属分析结论,记入 analysis_path[].conclusion 或 root_cause
      - description: <string>  # 做了什么改动
        from_analysis: <list[string]>  # 依据的分析线 id(analysis_path[].id,一条或多条)——
                                        # 分析出结论后为解决问题点而设计的手段,须能回溯到分析线
        dimension: <list[string], optional>
          # 优化维度,可多选: eliminate_redundancy / reuse_and_precompute / hide_latency / equivalent_substitution
        implementation_detail: <string>  # 具体代码层面怎么改的(文件、函数、改法)
        costs:  # 本优化点的成本(方案设计 + 实施 + 调试)
          input_cost: <float>   # 消耗输入 token 数, 单位 Million
          output_cost: <float>  # 消耗输出 token 数, 单位 Million
          time_hours: <float>   # 实际耗时, 单位小时
        equivalence_verification:
          method: <string>  # 怎么验证等价性的
          metrics: <list, optional>
            # 结构化的度量信息,便于检索
            - metric: <string>    # cosine_similarity / max_abs_diff / relative_error / kl_divergence / match_rate
              threshold: <string> # 如 ">= 0.9999" 或 "< 1e-4"
              value: <string>     # 实际值,如 "0.99999" 或 "3.2e-7"
          result: <string>  # 验证结论(通过/失败,可用文字补充细节)
        performance_result:
          metric: <string>  # 测量口径说明(benchmark 脚本/次数/中位数等)
          baseline_ref: <string, optional>
            # before 的参照对象,如 "raw model (无优化)" / "上一轮优化后" / "L0 基线"
          before_ms: <float>  # 该优化点单独 A/B 的 before,毫秒数值(单位固定 ms,非自由文本)
          after_ms: <float>   # 同口径 after,毫秒数值;accepted 必填且 **<= before_ms**
                               # (after > before = 性能回退, 校验直接打回);rejected 因精度
                               # 门禁先失败未获 A/B 时可省略
          verdict: <string>   # accepted / rejected / partial
        failure_reason: <string, optional>  # 如果 rejected,为什么失败

  final_state:
    adopted: <string>  # 最终采纳了哪个方案(或"无方案被采纳")
    files_modified: <list[string], optional>
      # 本案例修改/新增的文件列表
      # 如: ["mace/modules/fast_contraction.py", "scripts/benchmark_full.py"]
    end_to_end_before_ms: <float>  # 上一轮结束时的端到端(首轮 = 原始基线, 即 metadata.yaml
                                     # baselines.performance_ms, 校验强制相等)——单位固定 ms;
                                     # 多测试样本时取代表样本(canonical)的数值,
                                     # 辅样本数值留在 benchmark 原始输出与 test_data.characteristics
    end_to_end_after_ms: <float>   # 本轮各优化点全部合并后的端到端效果, 单位固定 ms, 同上取代表样本
    # 轮级加速比由脚本派生: 轮内增量 = before/after; 累计 = 基线/当轮 after, 两种口径在报告并列;
    # 各优化点的贡献率以总收益(基线 - 末轮 after)为分母计算。
    # 终局不标记——最后一个 round 文件即终点
    remaining_bottleneck: <string, optional>  # 优化后暴露的新瓶颈

  costs:
    # 本轮各阶段成本汇总(单位: token 为 Million, 时间为小时)
    # 与 optimization.attempts[].costs 对应: optimization = 各优化点成本之和(或统筹值)
    analysis:                # 根因分析阶段(profiling 采集解析 + 源码 Line A 分析)
      input_cost: <float>
      output_cost: <float>
      time_hours: <float>
    optimization:            # 优化方案得出与落实
      input_cost: <float>
      output_cost: <float>
      time_hours: <float>
    debugging:               # 额外工程调试(环境/工具链/非预期问题,不属于任何单一优化点)
      input_cost: <float>
      output_cost: <float>
      time_hours: <float>
    system:              # 除上述之外执行整个工作流需要的基础成本，总量减去其余三个
      input_cost: <float>
      output_cost: <float>
      time_hours: <float>
    total:                   # 总汇总 = 四节之和(校验强制)
      input_cost: <float>
      output_cost: <float>
      time_hours: <float>

  notes: <string, optional>
    # 补充说明。基于切分模型的优化案例,在此注明所依据的切分案例 id

  # 环境与基线信息(context)已移出案例文件: 准备阶段(Phase 1)生成 evidence_db/metadata.yaml,
  # 全轮共用,模板见 [metadata.md](metadata.md)。案例文件不再逐轮记录 hardware/软件栈/基线获取方式等。
```

## 切分案例文件 splitting.yaml

多卡切分案例（07_parallel_splitting 轨道）**单独落盘**为 `<workspace>/evidence_db/splitting.yaml`——与轮次案例分立，不占 `round_<N>.yaml` 命名；一个模型至多一个，重新切分时整体覆盖并在 `notes` 注明历史。

字段 = 案例通用字段（`phenomenon` / `analysis_path` / `root_cause` / `costs` / `notes` / `profiling_ref`，链路校验同样适用）+ `parallel_splitting` 节；**无** `optimization` / `final_state`（切分不是优化轮）。后续优化案例在 `notes` 引用本案例 id。

```yaml
- id: <string>
  # 格式: <模型名称>-splitting-<日期YYYYMMDD>
  # 例: "qwen3_7B-splitting-20260706"

  phenomenon: ...        # 同优化案例: 信号来自冒烟测试/OOM 分诊 + 源码结构提取
  analysis_path: ...     # 同优化案例: 分诊分析线 (id/from_signals/steps/conclusion)
  root_cause: ...        # 同优化案例: 容量根因聚合

  parallel_splitting:
    split_position: <string>  # input（输入边界）/ module（模型内部），见 07_parallel_splitting「切分怎么接入模型」
    split_dimension: <string>  # horizontal / vertical
    implementation_mode: <string>  # 输入边界编排 / monkey-patch / 子类覆写 / 源码内嵌（替换机制）
    tensor_distribution_table:
      # 逐张量声明分布方式。须覆盖所有大张量及其配套张量（mask/index/
      # position_ids 等）——配套漏切是实测高频错误；未声明 = 潜在 bug
      - tensor: <string>
        global_shape: <list[int]>
        distribution: <string>
        per_card_shape: <list[int]>
        consumer_op: <string>
        recovery_comm: <string>
    quantitative:
      single_card_peak_bytes: <int>
      per_card_peak_after_split_bytes: <int>
      hbm_bytes: <int>
      communication_volume_bytes: <int>
      communication_time_ms: <float>
      break_even: <string>  # 通信耗时 vs 计算节省的对比结论
    proofs:
      - operation: <string>
        equivalence_proof: <string>  # 切分等价性论证
        lower_bound_comm_bytes: <int>  # 理论通信下界
        actual_comm_bytes: <int>  # 实际通信量
        ratio: <float>  # actual / lower_bound，< 1.0 说明估算有误
    verification:
      method: <string>  # verify_split.py + split-type；缩配 baseline 注明缩配参数
      result: <string>  # verify_report 摘要（overall + 各 tier 结论）
    known_pitfalls:
      - pitfall: <string>
        mitigation: <string>
    numeric_sensitivity: <string, optional>
      # 数值敏感性评估结论（评测/RL 等敏感场景的验收策略与容差档选择，
      # 见 07 implementation_guide「验收标准」）

  costs:     # 切分工作总成本 (一次性结构变更, 无优化循环的阶段划分)
    input_cost: <float>
    output_cost: <float>
    time_hours: <float>
  notes: <string, optional>
```

## 填写原则

1. **完整优先**: 不确定某信息是否有用时,记下来。optional 字段能填就填。
2. **原文摘录**: phenomenon.signals.content 和 analysis_path[].steps.observation 尽量贴脚本原始输出或源码关键片段。
3. **失败必记**: optimization.attempts 中 rejected 的方案和 failure_reason 是最有价值的信息。
4. **切分与优化分开记录**: 多卡切分完成时写**切分案例**(独立文件 `evidence_db/splitting.yaml`,填 `parallel_splitting` 节,不占轮次命名);每经过一轮完整的 Phase 2->4 写一个**优化案例**(`round_<N>.yaml`)——一轮内的多个优化点**不拆成多个案例**,各记一条 `optimization.attempts[]`(每点带各自的 `from_analysis` 追溯、单独收益与成本),由 `final_state` 汇总。基于切分模型的优化案例,在 `notes` 注明所依据的切分案例 id。
5. **不强求归类**: bottleneck_type 能判断就写,判断不了写"mixed"并在 description 中说明;dimension 为列表可多选,仍判断不了则省略。
6. **结论必须带前提**: failure_reason 及一切"X 不可用 / X 劣于 Y"类结论须记录成立前提(图结构 / 环境变量 / dtype / 版本 / batch 规模)。用途有二:① 前提变化时旧结论须重验;② 死路清单(03「Level 分级」前置过滤)据此判断路径是永久死路还是前提已变的待重验项。无前提的结论按不可信处理。
7. **链路闭合**: `analysis_path[].from_signals` 与 `optimization.attempts[].from_analysis` 必填,且只能引用本案例内存在的 id——现象→分析→手段全链路在案例内可回溯,不依赖工作目录中间产物(candidates.md / solutions.md / signals.jsonl 不随案例库留存)。分析线 `conclusion` 须自含,不写 "RC1" 这类只在中间产物中有定义的代号。
8. **成本全程记录**: 根因分析(profiling+源码)、方案得出与落实、额外工程调试的成本(token + 时间)分阶段记于 `costs`,各优化点成本记于 `attempts[].costs`,总汇于 `costs.total`。
9. **环境元信息单点维护**: 模型/环境/基线信息只在 `evidence_db/metadata.yaml`(准备阶段生成,模板见 [metadata.md](metadata.md))记录一次,全轮共用,案例文件不重复记录。

## 目录结构

```
<workspace>/
├── profiling/           # profiling 数据
├── evidence_db/         # 案例库(项目工作目录下)
│   ├── metadata.yaml         # 模型/环境/基线元信息(准备阶段生成一次,全轮共用)
│   ├── splitting.yaml        # 切分案例(如有;07 轨道,与轮次案例分立)
│   ├── <round_x>.yaml        # 优化案例,每个案例一个文件,扁平存放
│   ├── summary.md / summary.json  # 总结报告(generate_summary.py 生成; json 为机器可读完整镜像,与 markdown 同源对齐)
└── ...
```

文件名 = 优化轮数 + `.yaml`
