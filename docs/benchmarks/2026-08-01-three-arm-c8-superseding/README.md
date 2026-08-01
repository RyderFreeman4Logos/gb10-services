# AEON 三臂基准对比报告（内容脱敏）

生成时间：`2026-08-01T04:13:35.420816Z`。

## 取代 2026-07-31 版本

本报告取代 [aeon-3arm-c8-20260731](https://github.com/RyderFreeman4Logos/gb10-services/releases/tag/aeon-3arm-c8-20260731)。上一版 A 使用 `aeon-legacy-bounded`，而不是预定的 `aeon-guard-max`。

全局 `retry.max_attempts=1` 把四级重试阶梯截断为一次尝试。因此，上一版 A 分数不能回答预定的 Guard 与 B/C 对比问题。

本次 A 包含重试阶梯修复 `cb08bb661bf7459953efa96cf18c21157798dfda` 和 c8 容量修复 `3595f27d92146a9014f237eb40221ac2264fef33`。

未发布的中间运行不作为基准数据。其 `max_in_flight=4`、并发 8 和 `queue=0` 产生 900 次 `queue_full_rejected` 观察及 225 行 infra。

## 结论：质量比较可用

三个 Pod ledger 均以 `succeeded` 结束，拥有完整的 290 个唯一案例且无 infra；三臂共有同一非数值 abstention `prose.ocean3`，质量与配对统计以其余精确数值交集为边界。

## 范围与口径

- 套件：`aeon-suite-v2`；固定套件哈希：`015c06c71ec7f162`；每臂计划案例：290。
- 路由标签：A = aeon-guard-max；B = aeon-direct-no-think；C = aeon-raw-max。共同请求参数：并发 8、temperature 0.6、top_p 0.95、top_k 20、max_tokens 50000。
- **质量**均值、median 和已评分 full-pass 率仅使用数值 score；**可用性**与计划案例 full-pass 率以全部计划案例为分母，未回答只计为非通过，不计为质量零分。
- 配对比较仅使用两个臂均有数值 score 的精确 case ID 交集；bootstrap 为 20,000 次配对百分位重采样（固定种子）。

## 每臂质量与可用性

| 臂 | Run | 账本状态 | 行数／计划 | 已评分 | 未回答 | 已评分均值 | 已评分 full-pass | 计划案例 full-pass |
|---|---|---|---:|---:|---:|---:|---:|---:|
| A (aeon-guard-max) | `guardmax-retry4-cap8-c8-20260801-001541` | succeeded | 290 / 290 | 289 | 1 | 0.9481 | 0.9481 | 0.9448 |
| B (aeon-direct-no-think) | `bc-rebench-20260729-B-c8` | succeeded | 290 / 290 | 289 | 1 | 0.9054 | 0.9031 | 0.9000 |
| C (aeon-raw-max) | `bc-rebench-20260729-C-c8-retry3` | succeeded | 290 / 290 | 289 | 1 | 0.9654 | 0.9654 | 0.9621 |

## 精确案例配对差距

| 比较（左 − 右） | 配对 N | 均值差 | 相对差（相对右） | 左胜／右胜／平 | 95% 配对 bootstrap CI | McNemar 精确 p（Holm） |
|---|---:|---:|---:|---:|---:|---:|
| A − B | 289 | 0.0427 | 4.71% | 16 / 3 / 270 | [0.0150, 0.0727] | 0.0044 (0.0089) |
| A − C | 289 | -0.0173 | -1.79% | 3 / 8 / 278 | [-0.0415, 0.0035] | 0.2266 (0.2266) |
| B − C | 289 | -0.0600 | -6.21% | 1 / 19 / 269 | [-0.0900, -0.0323] | 0.0000 (0.0001) |

三臂共同已评分交集：**289**。Cochran Q（full-pass 二元结果）：Q=20.7200，df=2，渐近 p=0.0000。

## 回答样本延迟（仅有 speed metadata 的已回答行）

| 臂 | e2e ms N / mean / p50 / p95 | TTFT ms N / mean / p50 / p95 | 客户端观测 decode tok/s 元数据 N / mean / p50 / p95 | 输出 tokens N / mean / p50 / p95 |
|---|---|---|---|---|
| A (aeon-guard-max) | 290 / 209275.50 / 110335.70 / 807897.22 | 288 / 199014.39 / 109668.42 / 702702.78 | 288 / 17054115.63 / 7436700.76 / 62435311.48 | 290 / 2903.53 / 1449.50 / 9915.30 |
| B (aeon-direct-no-think) | 290 / 20462.52 / 8316.30 / 66555.88 | 290 / 3207.86 / 1476.36 / 11156.59 | 290 / 172.46 / 32.15 / 46.22 | 290 / 550.33 / 192.00 / 1924.65 |
| C (aeon-raw-max) | 290 / 307161.28 / 195285.16 / 935919.77 | 285 / 297590.23 / 192806.84 / 887058.79 | 285 / 38548529.71 / 19202672.44 / 158477041.17 | 290 / 6518.94 / 2634.00 / 32799.75 |

## 证据边界与脱敏导出

- decode_tps is client-observed speed metadata without raw timing evidence in the public package and MUST NOT be interpreted as verified GPU/model generation throughput.
- `no_answer_n` 是没有数值 score 的终态结果数；本次完整 ledger 中它与 unscored 数相同。发布程序未读取或导出内容载荷、原始模型字段、blob、私钥、Pod 数据库或 runner 日志。
- 公开 ZIP 包含本报告、比较 JSON、脱敏 manifest、每臂的结构化结果 CSV 和 ZIP 内 manifest；不含数据库、端点地址或任何内容载荷。ZIP CRC 与成员路径已程序化检查。
- 没有将此临时运行目录或任何仓库变更提交到 Git。
