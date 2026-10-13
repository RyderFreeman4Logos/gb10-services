# Opt-in owned stack recovery

`gb10-stack-recovery@.service` is a disabled template, with no install target and
no enabled Guardian configuration. It requires the matching Guard escalation
protocol and a separately reviewed installation. Nothing here activates it.

Before activation, measure and approve a cold-stack startup admission floor on
the actual GB10. Put the resulting integer byte count in
`/home/obj/.config/gb10/stack-recovery.env` as
`GB10_STACK_STARTUP_MIN_AVAILABLE_BYTES`. There is deliberately no numeric default:
Docker caps are not measured residency, and Guardian's emergency threshold is
not startup headroom. Missing calibration fails before publishing readiness or
stopping any model. The calibrated floor must exceed the emergency threshold.

The installed coordinator and its existing `gb10_bounded_process.py` dependency
live in `/home/obj/.local/bin`; it reuses the installed `gb10_service_ready.sh`.
Keep the ordinary lifecycle and AEON local-recovery helpers at the matching
revision so they honor the persisted recovery-owner marker.

Guard creates a private, create-new manifest under
`%t/gb10-memory-guardian/recovery/<receipt_id>/manifest.json`. The directory is
0700 and protocol files are single-link owner-owned 0600 regular files.
The manifest binds boot, config revision, old target CID/cgroup device/inode,
emergency threshold and CLOCK_BOOTTIME deadline. The coordinator publishes
`ready.json` with the original manifest SHA-256 and its real systemd invocation.
It cannot mutate models until Guard validates that invocation and old target
and publishes byte-identical `permit.json`.

The coordinator holds the existing lifecycle mutex for the entire operation,
preserves investigation locks, and durably records its owner and mutation intent
before any systemd submission. Its journal is the content-free operation audit.
The recovery-owner marker also blocks manual operations after SIGKILL releases
the mutex and before manager-owned `ExecStopPost` reacquires it. Never delete a
stale marker merely to bypass this fence: inspect its owned journal first.

Only Ultimate text, Qwen embedding and Querit reranker are controlled. It stops
all three, waits for old containers/cgroups and stop jobs to disappear, then
requires both a measured increase in host MemAvailable and the calibrated floor.
It records before/released memory observations; an increase is a release check,
not a residency estimate or proof of future cold-start peak safety. It starts
embedding, reranker, then Ultimate, respecting existing unit verification and
readiness hooks. Each journaled start explicitly resets the unit's failure
counter before submission; any subsequent automatic restart is rejected. Real
inference probes and unchanged new unit/container generations are required for
all three before publishing `succeeded`.

The operation permits at most 3600 seconds and reserves 240 seconds for cleanup.
Cold startup is not guaranteed to finish in that budget. Cleanup may use a
bounded safety window after the operation deadline, but cannot publish an
accepted terminal result after that deadline. Unknown settlement retains the
owner marker and must remain unconfirmed at Guard.

Manager-owned cleanup requires the same manifest, invocation, permit and owner
marker. Before permit it never stops a model. After admission it settles owned
jobs/containers and publishes `stopped` only after verified absence. A successful
result published just before a crash is revalidated before clearing the marker;
cleanup does not stop that healthy stack. Outside-protocol direct systemctl or
Docker generation changes cause HOLD, not destructive cleanup of a replacement.

Guard must observe terminal unit/job state and the owned result. A real local
user-manager fixture showed that `systemctl --wait start` can return early when
a BindsTo stop cancels the start job while `ExecStopPost` is still running. A
waiter exit alone is not cleanup proof. The template binds to Guard so Guard
stopping also invokes bounded cleanup; successful inactive oneshots can clear
their InvocationID, so prove the invocation while it is live.

No production activation, calibrated GB10 cold-stack trial, or automatic recovery
claim follows from the isolated tests. Those remain separate operator acceptance.
