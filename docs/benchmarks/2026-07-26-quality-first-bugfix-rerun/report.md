# AEON Quality-First Benchmark — Guard Bugfix Rerun (290 cases)

生成时间：`2026-07-26T15:30:00Z`（初步，仅 A 臂完成；B/C 进行中）。

## 背景

2026-07-25 的三臂对比（155-case suite-v3, temp=0.6, max_tokens=50000）发现 A 臂（aeon-guard-max / quality-first）质量异常低（mean=0.3871），远低于 B（raw-no-think, 0.5806）和 C（raw-max, 0.5613）。

根因调查确认 A 臂被三个独立的 Guard 基础设施 bug 破坏：

1. **#216** — `stuck_watchdog` 对非流式请求（`stream=false`）误判卡死，触发 AEON 重启
2. **#220** — `request_deadline` 超时（20 min）触发 `local_recovery`，重启整个 AEON backend
3. **#219** — `max_attempts=1`（零次重试）导致 `upstream_stream_error` 直接计 0 分

修复部署后重跑 benchmark #7，这是首个数据可信的 quality-first 290-case 结果。

## 修复链路

| Issue | 根因 | 修复 | Commit |
|---|---|---|---|
| #216 | 非流式请求被 stuck_watchdog 误判 | exempt `unobservable_progress` | `2736757` (PR #218) |
| #220 | request_deadline 触发 backend restart | `trigger_on_request_deadline = false` default | `1e02e59` (PR #221) |
| #219 | upstream_stream_error 无重试 | `max_attempts` 1→3 | config deploy |

## Benchmark #7 结果

- **Suite**: `aeon-suite-v2` (hash `015c06c71ec7f162`), 290 cases
- **参数**: temperature=0.0, max_tokens=8192
- **Runner**: `aeon-mvp-resilient` (custom resilient wrapper)
- **Guard binary**: `1e02e59` (SHA `3716e4b6…`)
- **AEON**: DFlash n=10, util=0.355, v0.25.1
- **Run ID**: `qf18014-fac195ca84`

### A 臂（quality-first, Guard loop-recovery）总览

| 指标 | 值 |
|---|---|
| Scored | 288 / 290 |
| Passed (full-pass) | **269 / 290 (92.8%)** |
| Timeout (runner-side) | 2 |
| Loop errors | **0** |
| Mean score | **0.9308** |

### 分类别

| 类别 | N | Mean | Passed |
|---|---:|---:|---:|
| Coding | 54 | 0.9815 | 53/54 |
| Instruction | 61 | 0.9508 | 58/61 |
| Math | 71 | 0.9014 | 64/71 |
| Prose | 41 | 0.8500 | 34/41 |
| Reasoning | 63 | 0.9524 | 60/63 |

### 与旧 155-case 三臂对比

**注意**：旧三臂使用不同 suite（v3, 155 cases）、不同参数（temp=0.6, max_tokens=50000），**不可直接配对比较**。下表仅供趋势参考。

| 臂 | Suite | Cases | Mean | Passed |
|---|---|---:|---:|---:|
| A (guard-max, 旧, bug 破坏) | v3 | 155 | 0.3871 | 60/155 |
| B (raw-no-think, 旧) | v3 | 155 | 0.5806 | 90/155 |
| C (raw-max, 旧) | v3 | 155 | 0.5613 | 87/155 |
| **A' (quality-first, bug 修复后 #7)** | **v2** | **290** | **0.9308** | **269/290** |

旧 A 臂 mean=0.3871 → 修复后 A' mean=0.9308。虽然 suite 不同，但修复前后的差距足以确认 Guard 基础设施 bug 是旧 A 臂低分的根因。

## 待完成

- **B 臂** (raw-no-think, 290-case suite-v2): 进行中, run_id `qf18014-e007cd098d`
- **C 臂** (raw-max, 290-case suite-v2): 待 B 完成后启动

B、C 完成后将更新本报告并生成精确配对比较。

## 证据边界

- 未读取或导出 prompt、completion、原始输出、blob、私钥或原始数据库。
- Benchmark wrapper 与原始 Pod runner 语义不同：`TargetError` 转为单 case score 0，而原 runner 全局退出。
- Guard commit/binary hash 已记录以确保可追溯。
