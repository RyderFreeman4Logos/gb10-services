# SuperQwen official MTP K=5 + full-ISL — 16k unique-prompt reverse 32→1

Engine: vLLM SuperQwen3.8-27B NVFP4 + official MTP K=5, `--scheduler-reserve-full-isl`, `:18010`
Metric: wave `agg_decode_tok_s`
Validity: only waves with `n_ok=n` and `n_fail=0`
Prompt floor: 16000 tokens; `max_tokens=256`
Result: 32/32 waves, 528/528 units, status COMPLETE

## Reverse 32-1 (best c=17 = 15.55)

- c=32: 10.85
- c=31: 10.26
- c=30: 12.30
- c=29: 9.42
- c=28: 11.65
- c=27: 12.47
- c=26: 10.27
- c=25: 11.29
- c=24: 12.05
- c=23: 14.16
- c=22: 10.78
- c=21: 12.89
- c=20: 10.80
- c=19: 12.80
- c=18: 10.46
- c=17: 15.55
- c=16: 11.44
- c=15: 6.74
- c=14: 9.50
- c=13: 8.94
- c=12: 12.45
- c=11: 10.47
- c=10: 11.36
- c=9: 7.40
- c=8: 9.64
- c=7: 10.74
- c=6: 9.74
- c=5: 7.48
- c=4: 14.20
- c=3: 8.94
- c=2: 14.49
- c=1: 3.56

All 32 waves valid. Prior aborted pre-ISL 32→1 leftover is untracked and not part of this receipt.
