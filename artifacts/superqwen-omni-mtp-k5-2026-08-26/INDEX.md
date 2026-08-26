# SuperQwen AEON omni + MTP K=5 — 16k unique-prompt reverse 32→1

Engine: AEON omni `sha256:e62ac10d744ed7c8f3dd4d5631be0f7615870a88c327db9c1d382a27b36a61ee`, SuperQwen3.8-27B NVFP4, in-checkpoint MTP K=5, `--scheduler-reserve-full-isl`, `:18010`
Metric: wave `agg_decode_tok_s`
Validity: only waves with `n_ok=n` and `n_fail=0`
Prompt floor: 16000 tokens; `max_tokens=256`
Result: 32/32 waves, 528/528 units, status COMPLETE, 0 fail
Elapsed: 7762.8s

A prior 6-wave 32/16/8/4/2/1 run in this directory is contaminated (other-agent traffic) and is not this receipt.

## Reverse 32-1 (best c=21 = 16.64)

- c=32: 12.11
- c=31: 13.72
- c=30: 9.45
- c=29: 9.15
- c=28: 10.28
- c=27: 11.49
- c=26: 10.35
- c=25: 12.62
- c=24: 9.98
- c=23: 12.82
- c=22: 13.26
- c=21: 16.64
- c=20: 8.66
- c=19: 8.54
- c=18: 10.72
- c=17: 13.80
- c=16: 15.34
- c=15: 13.66
- c=14: 13.70
- c=13: 11.52
- c=12: 10.30
- c=11: 13.16
- c=10: 14.28
- c=9: 9.32
- c=8: 14.06
- c=7: 14.04
- c=6: 8.69
- c=5: 8.80
- c=4: 10.22
- c=3: 12.55
- c=2: 9.62
- c=1: 4.36

All 32 waves valid.
