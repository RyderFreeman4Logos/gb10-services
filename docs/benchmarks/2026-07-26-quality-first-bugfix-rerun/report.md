# AEON Three-Arm Quality Comparison — Guard Bugfix Wave

**状态：历史结果（published 0.0 wave）**

生成时间：`2026-07-27T18:30:00Z`

本目录是 **Guard #216/#220/#219 修复后**的 A/B/C 三臂完整发布包；其已发布
290-case wave 的 runner `temperature=0.0`，现在仅作历史证据，**不得作为下一次排名**。
下一次排名必须在同一 backend 上串行完整重跑 A/B/C，且三个 runner 都使用作者推荐的
`temperature=0.6`（Guard override 同时固定 `top_p=0.95`、`top_k=20`）。旧
155-case / suite-v3 / temp=0.6 结果同样**不可配对**，仅作历史背景。

## 结论（先说）

| 维度 | 排名 |
|---|---|
| **质量 mean** | **A = C (0.9308) > B (0.9054)** |
| **速度 e2e mean** | **B ≪ A ≪ C** |
| **输出 token mean** | **B ≪ A ≪ C** |

- **A（quality-first）与 C（raw-max）质量打平**（配对 n=289，A 赢 9 / C 赢 9 / 平 271）。
- **B（raw-no-think）质量明显更差**（A−B Δmean = +0.0254）。
- **速度上 A 远快于 C**：配对 e2e mean A 85.8s vs C 232.6s（A 快约 **2.7×**）；token mean A 1304 vs C 10078（A 约 **7.7× 更省**）。
  这与「A 会提前打断循环思考、C 可循环到耗尽 budget」的预期一致。
- **质量上 A 未压过 C**：loop-cut / CoT-salvage 节省了 context rot 的 token 与时间，但本 suite 上**净正确题数与 C 相同**。
  A 还多 2 个 runner `TimeoutError`（记 0 分），否则 mean 仍可与 C 持平或略高，但**不能**据此宣称 A 已在质量上胜出。

**默认路由建议**：质量上 A≈C；若同时要 **延迟与 token 成本**，**A 优于 C**；纯吞吐无 thinking 则 B 最快但质量最低。

## 1. 已发布的历史 evaluation contract

| 字段 | 值 |
|---|---|
| Suite | `aeon-suite-v2` |
| Suite hash | `015c06c71ec7f162` |
| Planned cases | 290 |
| Categories | Coding, Instruction, Math, Prose, Reasoning |
| temperature | 0.0（已发布历史 wave；下一次完整重跑必须为 0.6） |
| max_tokens | 8192 |
| Runner | `aeon-mvp-resilient`（见 `scripts/`） |
| Backend | AEON baseline dflash util=0.355, vLLM 0.25.1 |
| Guard binary | commit `1e02e59`, SHA prefix `3716e4b6` |
| 执行方式 | **串行**（共享同一 AEON backend / KV pool） |

### 臂变量（唯一 intentional 差异）

| 臂 | 路由 | 策略 |
|---|---|---|
| **A** | `:18014` quality-first | Guard：force thinking + loop detection + CoT salvage + ladder retry |
| **B** | `:18010` raw | `enable_thinking=false`，无 Guard 质量策略 |
| **C** | `:18015` raw-max | thinking on，无 Guard loop/quality 策略 |

### 前置 Guard 修复（使 A 臂数据可信）

| Issue | 根因 | 修复 |
|---|---|---|
| #216 | 非流式 stuck_watchdog 误杀 | PR #218 / `2736757` |
| #220 | request_deadline 触发 backend restart | PR #221 / `1e02e59`（`trigger_on_request_deadline=false`） |
| #219 | `upstream_stream_error` 零重试 | config `max_attempts` 1→3（根因仍 open） |

无效历史 run（勿与本波比较）：#4/#5/#6 被 watchdog/deadline/stream 误杀污染。

## 2. 总体质量

| 臂 | Run ID | Rows | Non-null scores | Passed | Mean | Infra loop_errors |
|---|---|---:|---:|---:|---:|---:|
| A quality-first | `qf18014-fac195ca84` | 290 | 289 | **269** | **0.9308** | 0 |
| B raw-no-think | `qf18014-e007cd098d` | 290 | 289 | 261 | 0.9054 | 0 |
| C raw-max | `qf18014-4501e9cd26` | 290 | 289 | **269** | **0.9308** | 0 |

- 三臂共有 1 个 `score=None`：`prose.ocean3`（需 frontier judge，未评分；mean 仅用 non-null）。
- A 另有 **2** 个 `gen_error: TimeoutError('timed out')`：`math.nt.factor.0001`、`reasoning.hard.dice-stop-expected-sum`（计 0 分）。
- B/C 全部 `status=scored`。

### 配对质量（双方 non-null，n=289）

| 对比 | Δmean | wins | losses | ties |
|---|---:|---:|---:|---:|
| A vs B | **+0.0254** | 14 | 7 | 268 |
| A vs C | **0.0000** | 9 | 9 | 271 |
| B vs C | **−0.0254** | 9 | 16 | 264 |

### 分项 mean / pass

| Category | A mean | A pass | B mean | B pass | C mean | C pass |
|---|---:|---:|---:|---:|---:|---:|
| Coding | 0.9815 | 53/54 | **1.0000** | **54/54** | 0.9630 | 52/54 |
| Instruction | **0.9508** | 58/61 | 0.8852 | 54/61 | **0.9508** | 58/61 |
| Math | 0.9014 | 64/71 | 0.9014 | 64/71 | **0.9155** | **65/71** |
| Prose | 0.8500 | 34/40 | 0.7167 | 28/40 | **0.8750** | **35/40** |
| Reasoning | 0.9524 | 60/63 | **0.9683** | **61/63** | 0.9365 | 59/63 |

解读：thinking（A/C）主要抬 **Instruction / Prose**；B 在 **Coding / Reasoning** 略有优势；Math 上 C 略高。

## 3. 速度与 token（验证「A 应比 C 快」）

| 臂 | e2e mean | e2e p50 | e2e p95 | tokens mean | tokens p50 |
|---|---:|---:|---:|---:|---:|
| A | **85.8 s** | 50.5 s | 281.7 s | **1304** | 418 |
| B | **14.2 s** | 5.6 s | 49.8 s | **487** | 191 |
| C | **236.6 s** | 94.2 s | 800.1 s | **10267** | 2711 |

**配对 A–C（n=288 双方有 e2e）**

| 指标 | A | C | 备注 |
|---|---:|---:|---|
| mean e2e | 85.8 s | 232.6 s | A 快 **~2.7×** |
| A_faster / C_faster | **224** | 64 | 多数 case A 更快 |
| mean output tokens | 1304 | 10078 | A 约 **7.7× 更省** |

结论：

1. **你的速度预期成立**：A 的 loop-cut / salvage 显著抑制了 C 那种「长循环 thinking 到 budget」的 token 膨胀。
2. **「无 context rot → 质量应更高」在本 suite 上未体现为 mean 优势**：时间与 token 收益明确，但 **正确题净结果与 C 打平**。可能原因见 §5。

## 4. 测试方法与脚本

### 4.1 方法

1. 部署 Guard `1e02e59` + `max_attempts=3` + `trigger_on_request_deadline=false`。
2. 确认 embedding / querit / AEON baseline / Guard active；listener smoke（真实 chat body，非仅 `/v1/models`）。
3. 同一 backend 上 **串行** 跑 A → B → C（C 曾被固件重启打断后全量重跑）。
4. Runner 对 `TargetError` 记 case-level 失败（label `loop_retry_exhausted`，**不等于** Guard loop detection），不全局 abort。
5. Evaluator 用 suite 内 checker；主观题无 frontier judge → `score=None`。
6. 聚合仅用 `case_id/status/score/speed`；**不导出** prompt、raw completion、blob、sqlite。

### 4.2 本目录文件

| 路径 | 内容 |
|---|---|
| `report.md` | 本报告 |
| `methodology.md` | 复现步骤与 contract 检查清单 |
| `improve_A.md` | A 臂质量抬升研究与假设 |
| `data/comparison.json` | 汇总 + 配对 + 速度 + 分项（红acted） |
| `data/comparison_ledger.json` | 全 case ledger（score/speed only，无 raw） |
| `scripts/run_bench_A_quality_first.py` | A 臂 runner 快照 |
| `scripts/run_bench_arm_B_C.py` | B/C 臂 runner 快照（`BENCH_ARM=B|C`） |

### 4.3 运行摘要

```bash
# A
AEON_DB=... AEON_BLOB_DIR=... python3 run_bench_A_quality_first.py
# B / C
BENCH_ARM=B AEON_DB=... AEON_BLOB_DIR=... python3 run_bench_arm_B_C.py
BENCH_ARM=C AEON_DB=... AEON_BLOB_DIR=... python3 run_bench_arm_B_C.py
```

脚本依赖 GB10 上 `Aeon-Bench-Pod-issue14/mvp` 与 Pod DB；发布包只保留方法快照，不包含私有 prompt/输出。

## 5. 为何 A 更快但未更高分？

**已证实**

- A token/e2e ≪ C → loop-cut 在 **效率** 上有效。
- A mean == C mean → loop-cut **没有**在本 290-case deterministic suite 上转化为额外 full-pass。

**最可能解释（按优先级）**

1. **Loop 触发稀疏**：多数 case 两边都正确且无进入 salvage；A/C 差异集中在 18 个分歧题，净胜负 9:9。
2. **Salvage 路径是双刃剑**：`on_reasoning_loop = bounded_answer_from_cot` + ladder 最终可落到 `force_disable` / 短 `max_tokens`。截断后的 CoT 可能 **丢掉 C 靠继续思考才得到的正确答案**，同时在另一些题上 **救回** C 因循环而烂掉的答案 → 净零。
3. **A 的 2 个 timeout 是纯损失**：C 在同题有答案（虽可能错）；A 直接 0 分。修 timeout / 请求 deadline 对齐可抬 A 的可用性分数。
4. **Checker 偏 final answer**：context rot 伤害的是长程一致性；本 suite 大量 numeric/regex checker 对「冗长但最终 box 对」仍给 1.0，弱化 A 的 rot-avoidance 优势。
5. **Param 契约曾不一致**：quality-first upstream 配置含 `temperature=0.6` 等 override，而已发布 runner 声明 temp=0.0。该 0.0 wave 因而仅保留为历史证据；下一排名必须完整重跑 A/B/C，并在 runner 与 Guard override 统一使用 temp=0.6（见 `improve_A.md`）。

## 6. 证据边界

- 未读取/导出 raw prompts、completions、blobs、credentials、源 sqlite。
- `decode_tps` 在 thinking 流上可能因 timing 定义失真；**排名以 e2e_ms 与 output_tokens 为准**。
- 已发布 290-case 三臂（runner temp=0.0）与旧 155-case 三臂（temp=0.6, max_tokens=50000）均仅历史参考，**不参与下一次排名**；下一排名须完整重跑 A/B/C（temp=0.6）。

## 7. 相关 commit（gb10-services 分支）

本波文档与运维配套（同分支 `chore/wip-quality-first-variant-recovery`）：

- Guard quality-first 配置 + max_attempts=3
- variant-aware AEON recovery
- hikv util 0.45 备选 unit
- 本 release 文档包
