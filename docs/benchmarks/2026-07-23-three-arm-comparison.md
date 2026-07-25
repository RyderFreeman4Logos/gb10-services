# AEON 三臂质量比较（最终发布索引，2026-07-25）

> **状态：最终、内容脱敏的质量比较。** 本页取代先前基于 174 个案例且受服务中断影响的草稿；草稿中的可用性数字不能用于质量排序。完整的发布文件和机器可读结果位于 [`2026-07-25-three-arm-rerun/`](2026-07-25-three-arm-rerun/)。

## 范围

- 固定套件：`aeon-suite-v3`，每个臂 155 个计划案例，全部完成且全部有数值评分。
- A：`aeon-guard-max`；B：`aeon-raw-no-think`；C：`aeon-raw-max`。
- 比较数据为内容无关的分数、状态、分类和速度元数据；未公开 prompt、completion、请求/响应载荷、数据库、blob、端点或凭据。

## 最终结果

| 臂 | 已评分／计划 | 质量均值 | 已评分 full-pass | 计划案例 full-pass |
|---|---:|---:|---:|---:|
| A — aeon-guard-max | 155 / 155 | 0.3871 | 0.3871 | 0.3871 |
| B — aeon-raw-no-think | 155 / 155 | 0.5806 | 0.5806 | 0.5806 |
| C — aeon-raw-max | 155 / 155 | 0.5613 | 0.5613 | 0.5613 |

精确 case-ID 配对（所有比较均为 155 对）：

| 比较（左 − 右） | 均值差 | 95% paired bootstrap CI | McNemar exact p（Holm） | 结论 |
|---|---:|---|---:|---|
| A − B | -0.1935 | [-0.2839, -0.1032] | 0.0001 (0.0002) | 显著 |
| A − C | -0.1742 | [-0.2774, -0.0710] | 0.0018 (0.0036) | 显著 |
| B − C | +0.0194 | [-0.0903, 0.1290] | 0.8176 (0.8176) | 不显著 |

## 解读边界

这是一项固定套件、固定路由标签和受控请求参数下的质量结果，不是对模型整体能力的普遍排序。三臂共享后端，延迟统计只作描述性证据。执行编排支持 A 的本地恢复，并使用有韧性的 wrapper；运行期间另有两次有序 AEON stop/start 事件，详见 2026-07-24 取证日记。尽管这些事件必须在解释中保留，三个最终 ledger 均以完整的 155/155 成功状态结束。

## 发布内容

- 英文发布说明与校验信息：[`2026-07-25-three-arm-rerun/README.md`](2026-07-25-three-arm-rerun/README.md)
- 内容脱敏报告：[`2026-07-25-three-arm-rerun/comparison_report.md`](2026-07-25-three-arm-rerun/comparison_report.md)
- 机器可读汇总：[`2026-07-25-three-arm-rerun/comparison.json`](2026-07-25-three-arm-rerun/comparison.json)
- 汇总收据：[`2026-07-25-three-arm-rerun/aggregate_receipt.json`](2026-07-25-three-arm-rerun/aggregate_receipt.json)
- 脱敏 ZIP：[`2026-07-25-three-arm-rerun/aeon_3arm_comparison_redacted.zip`](2026-07-25-three-arm-rerun/aeon_3arm_comparison_redacted.zip)
