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
bytes other than 64 lowercase hex plus newline, caps inspect JSON at
262144 bytes, and requires `State.Status` to be the string `exited`.
Publish is a same-directory `mv -Tf`; it is not a crash-durable fsync
transaction.
