# sparkDash (loopback :20080)

Read-only MiaAI-Lab/sparkDash dashboard for this GB10 host. Not a model unit.
Mutating HTTP (bench, shutdown, config, Hermes/Comfy actions) is rejected in
`createAuthMiddleware()`. The unit's `ExecStart` forces
`SPARKDASH_READ_ONLY=1`, so a preserved legacy/user env cannot reopen writes.
Loopback bind is not enough: tokenless loopback POSTs from a hostile Origin would otherwise start
real model work. Do not set a `SPARKDASH_TOKEN` in the tracked env.

## Pin

- Upstream: `https://github.com/MiaAI-Lab/sparkDash`
- Commit: `bbec3bb7886a95498cb495f574d1099a854f8e7f` (v1.8.6)
- Runtime: host Node (`mise node@22.23.2`), **not** privileged Docker
- Checkout: `/home/obj/src/sparkDash` (outside this repo)
- Bind: `127.0.0.1:20080` (`Linger=yes` user systemd)

## Why host process

Upstream Compose is `privileged: true`, `pid: host`, `/` bind-mount, and default port 5555. That is rejected here. The Node server already falls back from `/host/proc` to `/proc` when not in Docker.

## LLM ports

`config/sparks.json` probes raw vLLM on Tailnet:

| Port | Unit | Expected modelId |
|------|------|------------------|
| 18010 | `vllm-aeon-ultimate-uncensored-nvfp4` | `aeon` |
| 18012 | `vllm-embedding` | `qwen3-embedding-8b` |
| 18013 | `vllm-querit-4b-reranker` | `Querit/Querit-4B` |

Guard `:18009` is **not** an `llmPorts` target (not a generation engine). Its `/metrics` is Tailnet-only, same as the raw backends; loopback `:18009`/`:18010`/`:18012`/`:18013` are refused.

Overlay `profile/sparkdash/llmHost.js`: if `lanIp` is set, probe that host even when `isLocal` is true. Upstream probes `127.0.0.1` for local Sparks and would miss Tailnet-bound vLLM.

Disabled: Comfy cancel, Hermes update, decode/prefill/showcase benches, shutdown/WoL (`hermesMonitoring=false`, `comfyMonitoring=false`). The overlay still rejects those mutating routes server-side; hiding UI buttons is not the control. GET `/api/health` and metrics remain available.

`gpuMemoryUtilization=1` in sparkDash means engine-active/sleep flag, **not** AEON `--gpu-memory-utilization 0.515`.

## Install from this repo

```bash
# on GB10, after rsync of committed profile files
bash scripts/sparkdash_install.sh
systemctl --user daemon-reload
systemctl --user stop sparkdash.service
systemctl --user start sparkdash.service
```

Do not `Requires=` any vLLM/Guard unit. Do not restart models.

## Termux / phone

```bash
ssh -N -L20080:127.0.0.1:20080 obj@100.105.4.92
```

Open `http://127.0.0.1:20080`.

## Verify

```bash
systemctl --user is-active sparkdash.service
ss -ltn | awk '/:20080/'
curl -sS http://127.0.0.1:20080/api/health
curl -sS http://127.0.0.1:20080/api/sparks/gb10-promax/metrics
```

Expect `bindHost=127.0.0.1`, three `metrics.llm[]` entries with `available=true` and the model ids above. Idle `requestsRunning=0` is real idle, not fabricated.

For vLLM, each tok/s field includes an explicit state and age: `fresh` is a counter
advance; `stale` is a last observed window with its age; `unavailable` has no
recent measured window; and `idle` is a verified zero-running-request sample.
The overlay retains no rate beyond 30 seconds, does not infer generation from
prompt-only progress or `requestsRunning`, resets its window on counter reset,
and returns unavailable on a disconnected probe.

## Rollback / uninstall

```bash
systemctl --user stop sparkdash.service
systemctl --user disable sparkdash.service
rm -f ~/.config/systemd/user/sparkdash.service
rm -rf ~/.config/sparkdash
# optional: rm -rf /home/obj/src/sparkDash
systemctl --user daemon-reload
```

Does not touch model units or PIDs.

## Tracked source

- `profile/sparkdash/sparkdash.service` → `~/.config/systemd/user/sparkdash.service`
- `profile/sparkdash/sparkdash.env` → `~/.config/sparkdash/sparkdash.env`
- `profile/sparkdash/sparks.json` → `/home/obj/src/sparkDash/config/sparks.json`
- `profile/sparkdash/sparks.legacy-bbec3bb.json` — exact admitted pre-upgrade runtime state
- `profile/sparkdash/llmHost.js` → `/home/obj/src/sparkDash/server/collectors/llmHost.js`
- `profile/sparkdash/LlmProbe.js` → `/home/obj/src/sparkDash/server/collectors/LlmProbe.js`
- `profile/sparkdash/LlmDaily.js` → `/home/obj/src/sparkDash/server/collectors/LlmDaily.js`
- `profile/sparkdash/LlmPanel.tsx` → `/home/obj/src/sparkDash/src/components/SparkPage/LlmPanel.tsx`
- `profile/sparkdash/types.ts` → `/home/obj/src/sparkDash/src/api/types.ts`
- `profile/sparkdash/metricsStore.ts` → `/home/obj/src/sparkDash/src/hooks/metricsStore.ts`
- `profile/sparkdash/auth.js` → `/home/obj/src/sparkDash/server/auth.js`
- `profile/sparkdash/PIN`
- `scripts/sparkdash_install.sh`
