# Text-unit last-good inspect retention

Tracked Ultimate source now has `--enable-prefix-caching`, matching live APC.
Do not write, overlay, or `daemon-reload` the Ultimate unit until operators
first verify every live setting is retained. A later reload that drops any
live flag would start a new `Restart=always` generation with those settings
lost. This source alignment is not an automatic install.

## Helper-only stage (this repair)

This is the only executable install path in this runbook.

`unit_hooks_effective=false`. Helper install does not write a unit, does not
reload or restart, and does not change the current PID or container
generation. Stop hooks stay at the already-loaded definition.

```bash
bash scripts/gb10_install_retain_container_inspect.sh
```

The installer publishes `/home/obj/.local/bin/gb10_retain_container_inspect.sh`
mode `0755`. Read back owner, mode, and bytes against
`scripts/gb10_retain_container_inspect.sh` before claiming helper staging.

`docs/deployment/AGENTS.md` script provisioning does not install this helper
and is not a complete rebuild of text retain hooks. Use this runbook or
README Deployment Steps, which call the installer before any text unit.

## Unit-hook stage (not this repair)

Do not copy this block until every live setting is verified against the
committed Ultimate source, and until the selected fallback unit is
independently authorized. Reload is not restart. Do not enable, start, stop,
or switch text units here.

After helper byte/mode read-back, a selected unit can become effective only
with this order: unit file mode `0644` from the committed source, byte
identity of that leaf, `systemctl --user daemon-reload`, then
`systemctl --user show` of the loaded `ExecStop` / `ExecStopPost` argv
proving retain-before-cleanup. Sources:

- `profile/qwen3.6-27b-decensor-by-aeon/vllm-aeon-27b-dflash.service`
  (only after Ultimate is stopped)
- `profile/qwen3.8-27b-nvfp4-vllm/vllm-aeon-qwen38-dflash.service`
  (only after Ultimate is stopped)
- `profile/aeon-ultimate-uncensored-nvfp4/vllm-aeon-ultimate-uncensored-nvfp4.service`
  (only after operators first verify every live setting is retained)

`ExecStop=-` / `ExecStopPost=-` mean a retain skip or helper error must not
block `--cleanup`. Units wrap the helper with
`/usr/bin/timeout --signal=TERM --kill-after=2 10`, well under
`TimeoutStopSec=60`, so a blocked cid/JSON/FIFO path cannot consume the
stop budget. The helper also self-wraps that same deadline, rejects cid
bytes other than exactly 64 lowercase hex with an optional single newline,
caps inspect JSON at 262144 bytes, and requires `State.Status` to be `exited`.
Before runtime publication, the same helper synchronizes a content-free receipt
in the existing owner-only `~/.local/state/gb10-vllm-cids/` store. Receipts are
keyed by full CID and Docker `StartedAt`, with file and directory fsync; repeated
stop hooks preserve the first receipt. Capacity is 64 receipts: at capacity the
helper skips without deleting older evidence. A nonblocking directory lock
serializes count and publication; contention skips rather than delaying cleanup.
Runtime last-good inspect still
uses same-directory `mv -Tf` and is not itself a crash-durable transaction.

The durable receipt excludes Docker environment, labels, mounts and raw argv.
It records boot ID, timestamps, exited state/OOMKilled and the retainer PID/starttime;
the retainer is **not the SIGKILL initiator**. Three text units now call the same
helper once with `--snapshot-live` before readiness. This owner-alive baseline
binds full CID, Docker StartedAt, PID/starttime, canonical proc cgroup and scope
inode, rechecks Docker and owner identity, and durably publishes raw memory.events
in an immutable `<CID>.<StartedAt-hash>.memory.json` beside the exited archive.
There is no daemon, additional poller, high-frequency fsync, eviction or cap change;
capacity is 64 live snapshots plus 64 exited receipts under the same nonblocking lock.
Repeated hooks preserve the first baseline. A helper-only install does not execute
this new start hook on an already-running generation; no current coverage is implied.

A readable stop-time scope with that same baseline inode and unchanged exited
CID/StartedAt is marked `generation_fenced=true`, `terminal_status=captured`.
Otherwise the baseline is only `status=last-known`, `terminal_status=unavailable`:
its raw counters survive scope deletion, but events between the baseline and death
are **unknown**, even if the baseline already contains a nonzero oom_kill count.
Missing, malformed, unsafe or different-generation snapshots are never substituted;
legacy unfenced stop counters explicitly say `terminal_status=unfenced`.
Literal terminal-counter retention and next-natural-137 attribution remain PARTIAL
until a real same-generation death provides final counters and independent actor
or kernel evidence. One baseline does not reconstruct historical lost counters,
and simulated runtime-directory/cgroup loss is not an actual death or reboot test.
