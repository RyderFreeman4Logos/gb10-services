# AEON three-arm quality comparison — final redacted publication

This directory is the public, content-free publication for the final AEON three-arm comparison generated on 2026-07-25. It supersedes the interrupted preliminary 174-case comparison recorded in `../2026-07-23-three-arm-comparison.md`.

## Final result

All arms completed the same fixed `aeon-suite-v3` set with **155/155 numeric scores**:

| Arm | Route label | Mean score | Scored full-pass | Planned-case full-pass |
| --- | --- | ---: | ---: | ---: |
| A | `aeon-guard-max` | 0.3871 | 0.3871 | 0.3871 |
| B | `aeon-raw-no-think` | 0.5806 | 0.5806 | 0.5806 |
| C | `aeon-raw-max` | 0.5613 | 0.5613 | 0.5613 |

Exact case-ID paired comparisons (155 pairs each):

| Comparison (left − right) | Mean difference | 95% paired bootstrap CI | Holm-adjusted McNemar result |
| --- | ---: | --- | --- |
| A − B | -0.1935 | [-0.2839, -0.1032] | significant: 0.0002 |
| A − C | -0.1742 | [-0.2774, -0.0710] | significant: 0.0036 |
| B − C | +0.0194 | [-0.0903, 0.1290] | not significant: 0.8176 |

## Interpretation and caveats

These are quality results for this fixed suite, route labels, and controlled request settings—not a general capability ranking. The arms shared a backend, so latency values are descriptive rather than independent performance measurements. Orchestration included a local resume for Arm A and used a resilient wrapper. Two orderly AEON stop/start incidents occurred during the broader run window and are documented in the 2026-07-24 forensics diary; each final ledger nevertheless completed successfully with 155 rows.

## Published files

| File | Purpose | SHA-256 |
| --- | --- | --- |
| `comparison_report.md` | Human-readable redacted report (Chinese) | `1d476b8ddd41a6ccc79261db76e3a7e98dd52e055fae9119291a7f78dc432c46` |
| `comparison.json` | Machine-readable aggregate and paired statistics | `2ac1bffff618d7a4c80267f71ff8ed2c192270bc20002efa900321b28e9bf003` |
| `aggregate_receipt.json` | Export-policy and completion receipt | `da0c1bda6bc00bc5e11e7c452eb7d56a421335ef73dd4fcdee0e54b10abc13c4` |
| `aeon_3arm_comparison_redacted.zip` | Redacted CSVs, report, JSON, and ZIP manifests | `c88e93e578b2a7fd047d61daa783a7b9100ee79010a7a0f6ea7f47cac205f221` |

The ZIP contains only three `sanitized_results.csv` files, the report, comparison JSON, and manifests. Its CSV headers are limited to case ID, category/tier, result status/score, and speed/output-count metadata.

## Redaction policy

No prompts, completions, request or response payloads, raw outputs, databases, blob stores, endpoint addresses, credentials, or private runtime logs are included. The source package was checked for ZIP member paths, allowed file types, CRC validity, prohibited CSV columns, URLs/IP addresses, and credential-like fields before this publication.
