# AEON Ultimate DFlash 32-to-1 Throughput Sweep

## Run identity and invariants

- Run ID: `97d3e34d-ef4d-4955-9919-5c7190ec99fe`
- Status: **COMPLETE** (`complete=true`)
- Config epoch: `923c3ea43ab4a558b97f774eb436fa80e367299816c9d3a2653847a99adac466`
- Endpoint: `http://100.105.4.92:18010/v1`
- Model: `abliterated-qwen-latest-27b-nvfp4`
- Source commit/tree: `f368f3cd67348b723c68d27c4924a0778c874461` / `365fa05e4be6970ec08da6f4de63eb684c1cedc5`
- Completion: `2026-08-30T13:44:45.719Z` (state `started_at + elapsed_s`)
- Workload order: `[32, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]` (32 waves, descending 32 to 1)
- Privacy boundary: request records retain metadata and token/timing counters only; no prompt, completion, message, reasoning, or raw payload fields are retained.

## Overall summary

- `528/528` work units succeeded; failures: `0`.
- Completion tokens: `98,298`; finish reason `length`: `344`.
- Deadline-exceeded waves: `0`; retry-exhausted requests: `0`.
- Elapsed: `10531.697083s`.

## Post-run health

- Guard HTTP: `200`; raw HTTP: `200`.
- PIDs remained unchanged with zero restarts: Guard `1892322`, AEON `2798410`, embedding `3950630`, Querit `850484`.
- AEON swap current/max: `0 / 0` bytes.

## Wave metrics

| Concurrency | n_ok | n_fail | n_finish_length | wave_wall_s | aggregate prompt tok/s | aggregate decode tok/s | completion tokens |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 32 | 32 | 0 | 21 | 573.838210 | 899.758836 | 10.410600 | 5974 |
| 31 | 31 | 0 | 20 | 546.877067 | 914.647971 | 10.695274 | 5849 |
| 30 | 30 | 0 | 18 | 829.253955 | 583.705386 | 6.584231 | 5460 |
| 29 | 29 | 0 | 18 | 564.489715 | 828.886669 | 9.353581 | 5280 |
| 28 | 28 | 0 | 21 | 509.494764 | 886.698023 | 11.452522 | 5835 |
| 27 | 27 | 0 | 15 | 470.060646 | 926.765522 | 9.620035 | 4522 |
| 26 | 26 | 0 | 18 | 464.602638 | 902.930732 | 11.071827 | 5144 |
| 25 | 25 | 0 | 17 | 442.123859 | 912.350673 | 10.813712 | 4781 |
| 24 | 24 | 0 | 14 | 423.787900 | 913.626840 | 9.679370 | 4102 |
| 23 | 23 | 0 | 16 | 690.996749 | 537.041601 | 6.442867 | 4452 |
| 22 | 22 | 0 | 17 | 440.360814 | 806.143482 | 10.525460 | 4635 |
| 21 | 21 | 0 | 15 | 402.728894 | 841.350112 | 10.269936 | 4136 |
| 20 | 20 | 0 | 14 | 357.414321 | 903.052790 | 11.065589 | 3955 |
| 19 | 19 | 0 | 12 | 343.801423 | 891.700789 | 10.005776 | 3440 |
| 18 | 18 | 0 | 12 | 329.095243 | 882.522633 | 10.109535 | 3327 |
| 17 | 17 | 0 | 11 | 307.159739 | 893.072772 | 10.007822 | 3074 |
| 16 | 16 | 0 | 10 | 287.940527 | 896.601819 | 9.932607 | 2860 |
| 15 | 15 | 0 | 13 | 273.712008 | 884.287110 | 12.630063 | 3457 |
| 14 | 14 | 0 | 8 | 253.386200 | 891.571837 | 9.250701 | 2344 |
| 13 | 13 | 0 | 11 | 531.986471 | 394.273184 | 5.415551 | 2881 |
| 12 | 12 | 0 | 9 | 242.528470 | 798.429973 | 10.011196 | 2428 |
| 11 | 11 | 0 | 4 | 209.660281 | 846.607662 | 6.353135 | 1332 |
| 10 | 10 | 0 | 6 | 203.954114 | 791.040677 | 8.188116 | 1670 |
| 9 | 9 | 0 | 2 | 168.941406 | 859.457745 | 4.871511 | 823 |
| 8 | 8 | 0 | 5 | 147.323752 | 876.124848 | 9.591121 | 1413 |
| 7 | 7 | 0 | 3 | 125.018822 | 903.479956 | 8.342744 | 1043 |
| 6 | 6 | 0 | 3 | 108.966612 | 888.565753 | 9.498322 | 1035 |
| 5 | 5 | 0 | 5 | 94.887233 | 850.103828 | 13.489697 | 1280 |
| 4 | 4 | 0 | 3 | 72.207671 | 893.810848 | 10.705234 | 773 |
| 3 | 3 | 0 | 1 | 55.904332 | 865.872071 | 7.566498 | 423 |
| 2 | 2 | 0 | 2 | 39.048793 | 826.401980 | 13.111801 | 512 |
| 1 | 1 | 0 | 0 | 18.519039 | 870.995535 | 3.131912 | 58 |

## Throughput notes

- **Best aggregate decode throughput:** concurrency `5`, `13.489697` tok/s (wave completion tokens `1280`).
- **Materially low-throughput waves (descriptive only):** using the reproducible rule `aggregate decode tok/s <= 50% of the 9.969191 tok/s median` (cutoff `4.984596`), concurrency values `[9, 1]` qualify. No cause is inferred or claimed.
