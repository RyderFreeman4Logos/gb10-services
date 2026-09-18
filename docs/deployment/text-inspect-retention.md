# Text-unit last-good inspect retention

Install the helper **before** any text unit that calls it on stop. The units
use the fixed path `/home/obj/.local/bin/gb10_retain_container_inspect.sh`.
A missing helper makes the first `ExecStop` fail; the `-` prefix still lets
generation-safe cleanup run, but the inspect file is not updated.

```bash
bash scripts/gb10_install_retain_container_inspect.sh
```

Then install the unit that will own `:18010`. Do not enable more than one of
these text units at once.

```bash
install -m 0644 profile/aeon-ultimate-uncensored-nvfp4/vllm-aeon-ultimate-uncensored-nvfp4.service \
  /home/obj/.config/systemd/user/
```

27B DFlash fallback (only after Ultimate is stopped):

```bash
bash scripts/gb10_install_retain_container_inspect.sh
install -m 0644 profile/qwen3.6-27b-decensor-by-aeon/vllm-aeon-27b-dflash.service \
  /home/obj/.config/systemd/user/
```

Qwen38 / SuperQwen3.8 fallback (only after Ultimate is stopped):

```bash
bash scripts/gb10_install_retain_container_inspect.sh
install -m 0644 profile/qwen3.8-27b-nvfp4-vllm/vllm-aeon-qwen38-dflash.service \
  /home/obj/.config/systemd/user/
```

`ExecStop=-` / `ExecStopPost=-` mean a retain skip or helper error must not
block `--cleanup`. The helper itself bounds `docker inspect` to 8 seconds,
keeps the previous JSON on timeout/failure, and replaces the output leaf only
when it is absent or an owner-owned regular file. That replace is a same-
directory `mv -Tf`; it is not a crash-durable fsync transaction.

## Live APC is a separate deploy gate

Tracked Ultimate source currently has `--no-enable-prefix-caching`. Live APC
is already enabled. Installing the helper alone does not change the running
generation. Installing the Ultimate unit and `daemon-reload` updates the next
stop/start definition; the next natural `Restart=always` would start APC-off.
Do not treat whole-unit install as this inspect fix. Align source APC in a
separate reviewed commit before an authorized unit+reload.
