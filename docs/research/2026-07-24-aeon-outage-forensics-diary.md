# AEON 中断取证日记（2026-07-24）

- **主机：** `obj@gb10`（取证时远端主机名 `promaxgb10-e659`；服务为用户单元）
- **目标单元：** `vllm-aeon-27b-dflash.service`
- **事件窗口：** 2026-07-24 06:20–07:00 PDT
- **新鲜取证时间：** 约 07:01–07:04 PDT
- **范围：** 仅内容无关的只读取证。未读取 prompt、completion、请求体或凭据；未执行 `stop`、`start`、`restart`、`kill`，也没有改动任何服务配置。
- **与最终基准的关系：** 本文是中断取证时间线；其中的进度/均值是事件当时的快照，不是最终质量比较。最终的完整三臂结果见 [`../benchmarks/2026-07-25-three-arm-rerun/README.md`](../benchmarks/2026-07-25-three-arm-rerun/README.md)。

## 结论摘要

这是一次**已被 systemd 明确标记为“Stopping”的有序生命周期中断**，而不是目前证据所支持的 AEON 自崩溃或 OOM 事件：

1. `06:33:41`，user systemd 首先记录 `Stopping vllm-aeon-27b-dflash.service`；同一秒 Docker 内的 EngineCore 记录收到 `SIGTERM`。
2. `06:34:02`，systemd 记录 cleanup/control process `status=1`、main process `status=137`、`Failed with result 'exit-code'`、`Stopped`，随后立即 `Starting` 新一代。
3. `06:34:36` 新一代开始载入模型；`06:50:51` vLLM server 开始监听；`06:51:02` unit 记录 `Started` / ready。当前单元为 `active (running)`，`NRestarts=0`。
4. Guard 在停机后的 `06:34:02` 仅记录旧 AEON guardian registration 已不存在、随即发布新容器 cgroup；这在时序上是停机/新容器创建的**结果**，不是 Guard 已发起恢复的审计证据。

因此，已经证明的机制是**外部生命周期操作**；仍未能从保留的 user journal 中归因具体发起者。

## 精确时间线（PDT）

| 时间 | 只读、内容无关的证据 | 含义 |
|---|---|---|
| 06:20–06:33 | AEON journal 仍有成功响应与 Engine throughput/运行请求指标。 | 中断前 backend 正在服务，不是早已停止。 |
| 06:33:37 | `sysmon`: `mem_used_mb=95527`、`swap_used_mb=423`、swap-in/out 均为 `0.00 MiB/s`、GPU util `96%`。 | 停止前主机没有对应的 swap I/O 突发。 |
| 06:33:41 | user systemd: `Stopping ...`; EngineCore: `trigger received signal=SIGTERM`。 | 信号来自已进入 stop 路径的生命周期操作。 |
| 06:33:42 | `sysmon`: 主机已用内存降至 `92525 MiB`；swap 仍为 `423 MiB`，swap-in/out 仍为零。 | 与开始销毁 AEON generation 的时间一致。 |
| 06:34:01 | Guard request-cleanup 日志出现上游流错误与 HTTP 502。 | 是 AEON 不可用的影响信号，不证明谁发起 stop。 |
| 06:34:02 | systemd: control process `1/FAILURE`、main `137`、`Stopped`，紧接着 `Starting`；Guard 记录旧 guardian registration 不存在并发布新 cgroup。 | 停止已完成后，新的 generation 被创建。 |
| 06:34:03 | `sysmon`: `mem_used_mb=45980`，swap 仍 `423 MiB`，swap-in/out 均为零。 | 大幅内存释放发生在有序停止之后；没有可见 swap/OOM 序列。 |
| 06:34:36 | EngineCore: `Starting to load model`。 | 冷启动/载模开始。 |
| 06:50:51 | API server: `Starting vLLM server`。 | backend 开始监听。 |
| 06:51:02 | `gb10_service_ready.sh`: `SERVICE_READY elapsed=1019s`; systemd: `Started`。 | replacement generation 达到 unit-ready。 |

已知基准影响（由父级上下文提供，未在本次读取任何请求内容）：Arm A 在 **76/155** 时因 Guard `502 upstream_transport_error` 失败；Arm C 继续运行；monitor 已退出。

## 新鲜只读证据

### 生命周期与恢复形态

当前有效 unit 属性为：

- `ActiveState=active`，`SubState=running`，`MainPID=248431`；
- 新 generation 的 `ExecMainStartTimestamp=06:34:02 PDT`、`ActiveEnterTimestamp=06:51:02 PDT`、`NRestarts=0`；
- `Restart=always`，但 `RestartUSec=30s`；本事件是 `Stopping → SIGTERM → Stopped → 立即 Starting`，不是先出现未预期主进程退出、再等待自动重启延迟的典型形态；
- `ExecStop`/`ExecStopPost` 为 Docker cleanup/no-swap verifier。它解释了 stop 期间 control process 的失败记录，但不能把 `137` 自动归类为 OOM。

Guard 的已部署配置确实启用了：

- `[upstream.local_recovery]`，其 `restart_command` 指向受控的 `aeon_text_stop_start.sh` 辅助程序；
- `[upstream.stuck_watchdog]`，`check_interval_secs=30`、`detection_window_secs=180`。

对该 wrapper 的新鲜只读检查显示它严格执行：

```text
systemctl --user stop vllm-aeon-27b-dflash.service
systemctl --user start vllm-aeon-27b-dflash.service
```

故它的**外观**与观察到的 stop/start 序列相符。可是，在 06:20–07:00 的 Guard/user journal 中没有发现 `local_recovery`、`stuck_watchdog`、`aeon_text_stop_start` 或 recovery-success/failure 的发起记录；唯一 guardian 相关行位于 06:34:02，且语义为新容器 cgroup 注册。这个缺口不允许把 Guard 标为已证实的操作者。

### OOM、主机内存与 cgroup

- 新鲜 `journalctl -k` 的 OOM 关键词查询未返回匹配项；该用户缺少查看其他用户/系统 journal 的权限，因此这是**受可见性限制的阴性证据**，不能声称穷尽了全部内核日志。
- 可读取的 `dmesg` OOM 关键词查询同样没有返回匹配项。
- 事件附近 sysmon 样本中，host swap 固定为 `423 MiB`，所选停机、冷启动和 ready 时点的 `swap_in_mb_s`/`swap_out_mb_s` 都是 `0.00`。它不支持“当时发生主机 swap 风暴导致 OOM”的解释。
- 07:01 PDT 左右主机快照为：约 `89 GiB` used、`31 GiB` available、`423 MiB` swap used（15 GiB swap device）。这是事后快照，不可反推事件瞬间的所有内核状态。
- 当前 replacement 容器 `32b859…` 的精确 Docker scope cgroup 显示：`memory.current=8191369216`、`memory.peak=22832250880`、`memory.max=137438953472`、`memory.swap.current=0`、`memory.swap.max=0`，且 `memory.events` 与 `memory.events.local` 的 `oom`、`oom_kill`、`oom_group_kill` 全为 `0`。Docker inspect 同时报告当前 replacement `oom_killed=false`。

最后一项只证明**06:34 后 replacement generation**的 cgroup/no-swap 状态；被 `--rm` 删除的旧容器和旧 cgroup 已不再可查，不能把它误用为对旧 generation 的绝对 OOM 反证。

## 根因假设排序

下列是对**发起者**的工作概率排序（总计 100%，用于后续取证优先级，而非已经证明的归因）。对“发生了有序外部生命周期操作”这一机制本身，置信度为**高**。

| 排名 | 假设 | 工作置信度 | 依据与限制 |
|---:|---|---:|---|
| 1 | 非 Guard 特定的外部 `systemctl` / 生命周期脚本（人工或其他自动化） | **55%** | `Stopping` 先于 SIGTERM，随后是同一 lifecycle transaction 中的 `Stopped/Starting`；这最直接支持一个外部 unit 操作。user journal 没有记录调用者身份。 |
| 2 | Guard `local_recovery` 或 stuck-watchdog 调用其 `stop`+`start` wrapper | **25%** | 配置已启用，且 wrapper 的确是该 stop/start 形态；但没有 Guard recovery/watchdog 发起、执行或结果日志，06:34:02 guardian 行更像后果。 |
| 3 | 其他未留痕的控制器/未知生命周期来源 | **15%** | user journal 无法给出 systemctl 调用者；kernel journal 也受权限限制。 |
| 4 | OOM 或 AEON 自崩溃后恢复 | **5%** | 与 `Stopping → SIGTERM` 的顺序不符；所见 OOM 查询、swap I/O、替代 generation cgroup 也未给出正证据。不过旧 `--rm` container/cgroup 已消失，故不能降为零。 |

## 影响、边界与后续取证建议

- Guard、embedding 和 canonical Querit reranker 在本次新鲜 service inventory 中都为 `active (running)`；AEON replacement 同样 active。未对任何服务做生命周期操作。
- 本记录不把 Guard 的代理 502 当作根因：它们发生在 AEON stop/load 窗口中，是下游可观察到的可用性后果。
- 若要把排名 1/2 变为可审计结论，下一次任何 AEON lifecycle wrapper 都应在调用前后写入内容无关的审计元组：**actor/controller、reason/incident ID、目标 unit、开始/结束时间、systemd invocation ID、raw-health 结果**。这是一项后续改进建议，不是本次执行的变更。
- 不应通过重启 Guard 或其他健康服务来“验证”该假设；那会破坏现有取证边界。

## 本次取证命令类别

- `systemctl --user show` / `list-units`：单元状态、重启策略、有效 ExecStart/ExecStop、cgroup；
- `journalctl --user`：AEON、Guard、memory guard 及生命周期/guardian 关键词，限定 06:20–07:00 PDT；
- `journalctl -k` 与 `dmesg` 的 OOM 关键词只读查询；
- rootless Docker `inspect` 与当前精确 Docker scope 的 cgroup-v2 `memory.*`/`memory.events*`；
- `free`、`/proc/swaps` 与保留的 `sysmon_2026-07-24.csv` 事件对齐样本；
- Guard recovery wrapper 源码的只读检查。

所有上述操作均为读取；本次日记没有触发服务重启、停止、启动或 benchmark 进程操作。

## 第二次事件附录：17:33 PDT 有序 stop/start（仅内容无关取证）

- **事件窗口：** 2026-07-24 17:20–18:00 PDT；以下新鲜快照采集于约 17:41 PDT。
- **范围：** 仅读取 user journal、可见的 kernel journal/dmesg、cgroup-v2、Docker inspect、`sysmon` 与主机内存状态；未读取 prompt、completion、请求体或凭据，也未发出任何服务或 benchmark 的生命周期命令。

### 第二次时间线（PDT）

| 时间 | 只读、内容无关的证据 | 含义 |
|---|---|---|
| 17:33:40 | `sysmon`: `mem_used_mb=94333`、`swap_used_mb=423`、`swap_in_mb_s=0.00`、`swap_out_mb_s=0.00`、GPU util `96%`。 | 停止前后没有可见的主机 swap I/O 突发。 |
| 17:33:41.603 | user systemd 首先记录 `Stopping vllm-aeon-27b-dflash.service`；随后容器 API server 记录其 engine-client 关闭序列。 | 仍是已进入 lifecycle stop 路径的有序中断，而不是先观察到 AEON 自发退出。 |
| 17:33:42 | `sysmon` 已为 `mem_used_mb=49338`；swap 仍为 `423 MiB`、swap-in/out 均为 `0.00 MiB/s`。 | 主机已用内存的大幅释放紧跟 stop 开始，与 AEON teardown 的时序相符；这一序列不支持可见的 swap 风暴。 |
| 17:34:02.257 | Guard 仅记录 memory guardian 无法打开 `aeon-text` 的 cgroup directory。 | 这是正在销毁/已消失的旧 cgroup 的观察结果；它不是 Guard 发起 local-recovery 的审计记录。 |
| 17:34:02.284–.449 | systemd 记录 cleanup control process `1/FAILURE`、main process `137`、`Failed with result 'exit-code'`、`Stopped`。 | stop/cleanup 的结果；`137` 在此时序中不能单独归因为 OOM。 |
| 17:34:02.454 | systemd 紧接记录 `Starting` 新 generation；新 Docker scope 随后创建。 | 与显式 `stop` 后立即 `start` 的形态一致，而不是可见的延迟式自动重启。 |
| 17:34:36 | EngineCore 记录开始载入模型。 | replacement generation 已进入冷启动/载模阶段。 |

父级在检测时提供的内容无关基准影响为：Arm A 已成功 `155/155`（mean `0.387`）；Arm C 仍存活于 `113/155`（mean `0.522`）；monitor PID `3896494` 仍存活。该数据未通过本次取证读取请求内容。

### Guard、内核与内存边界

- 对 `llm-guard-proxy` 在整个 17:20–18:00 PDT 窗口的定向审计，没有 `local_recovery`、`stuck_watchdog`、`aeon_text_stop_start` 或 recovery attempt/success/failure 行。上述唯一 Guard 行是旧 cgroup 不可打开，时间上位于 systemd 已记录 `Stopping` 之后、`Stopped` 之前，故仅能证明 Guard 看到了 cgroup 消失。
- `journalctl -k` 明确提示该账户看不到其他用户和 system 的 journal；`dmesg -T` 被拒绝（不允许的操作）。因此本次无法用全量 kernel 日志排除旧 generation 的 OOM，不能把“没有可读 OOM 行”误记为 kernel 阴性结论。
- 17:41 PDT 主机快照：`MemAvailable=41985264 kB`（`free` 显示约 `40 GiB` available），swap 已用 `423 MiB`。AEON 当时为 `ActiveState=activating`、`SubState=start-post`、`ExecMainStartTimestamp=17:34:02 PDT`、`NRestarts=0`；Guard、embedding 与 reranker 均仍为 `active (running)`。
- 当前 replacement Docker scope（container `eb9ff…`）只读计数为：`memory.current=7337193472`、`memory.peak=23889358848`、`memory.max=137438953472`、`memory.swap.current=0`，`memory.events{,.local}` 的 `oom`、`oom_kill`、`oom_group_kill` 都为 `0`；Docker inspect 为 `OOMKilled=false`。这些是 **17:34 后 replacement** 的状态，不能反证已被删除的旧 container/cgroup。

### 第二次事件的发起者假设排序

对“外部有序 lifecycle 操作已经发生”的机制仍为**高置信度**。下列百分比仅是后续取证优先级：

| 排名 | 假设 | 工作置信度 | 新证据与限制 |
|---:|---|---:|---|
| 1 | 非 Guard 特定的外部 `systemctl` / 生命周期脚本（人工或其他自动化） | **60%** | `Stopping` 在 container 关闭与 `137` 前出现，随后无等待地转入 `Starting`；这两次重复的 stop/start 形态最直接支持外部 lifecycle 操作，但 user journal 不记录调用者身份。 |
| 2 | Guard `local_recovery` 或 stuck-watchdog 调用其 stop+start wrapper | **20%** | 已部署能力仍使其外观相符；但本窗口没有任何直接的 recovery/watchdog 发起或结果行。17:34:02 的 Guard cgroup 行是 stop 过程中的观察结果，不能升级为操作者证据。 |
| 3 | 其他未留痕的控制器/未知 lifecycle 来源 | **15%** | user journal 缺少 caller identity，且 system/kernel journal 对当前账户不可见。 |
| 4 | OOM 或 AEON 自崩溃后恢复 | **5%** | 与 `Stopping → 有序关闭 → Stopped → Starting` 的可见顺序不符，且 sysmon 未显示 swap I/O 突发；不过旧 `--rm` container/cgroup 已消失、kernel 日志不可见，不能降为零。 |

本附录没有执行 recovery、服务重启、停止、启动、kill 或 benchmark 操作。
