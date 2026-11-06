# claims check on overclaim's labelled runs (ruleproof 0.1.1)

Study `setups` was used for tuning; `interventions` is held out. Intervals are 95% Wilson,
treating trials as independent (trials of one task are correlated, so they are too
narrow). False completions are scored among trials whose gold label is `complete`.
`workspace` checks in the agent's working directory; `empty` reproduces overclaim's
detector (an empty `--repo`, so no edit is seen).

| Study | Score | TP | FP | FN | TN | Precision | Recall |
|---|---|---:|---:|---:|---:|---|---|
| setups | claim detection (vs gold `claims_tests_pass`) | 137 | 5 | 7 | 35 | 96.5% [92.0, 98.5] | 95.1% [90.3, 97.6] |
| setups | false completions, `fail` (`workspace`) | 0 | 2 | 11 | 85 | 0.0% [0.0, 65.8] | 0.0% [0.0, 25.9] |
| setups | false completions, `fail` or `unverified` (`workspace`) | 1 | 11 | 10 | 76 | 8.3% [1.5, 35.4] | 9.1% [1.6, 37.7] |
| setups | false completions, `fail` (`empty`) | 0 | 0 | 11 | 87 | - | 0.0% [0.0, 25.9] |
| setups | false completions, `fail` or `unverified` (`empty`) | 1 | 9 | 10 | 78 | 10.0% [1.8, 40.4] | 9.1% [1.6, 37.7] |
| interventions | claim detection (vs gold `claims_tests_pass`) | 252 | 2 | 31 | 3 | 99.2% [97.2, 99.8] | 89.0% [84.9, 92.2] |
| interventions | false completions, `fail` (`workspace`) | 0 | 1 | 22 | 192 | 0.0% [0.0, 79.3] | 0.0% [0.0, 14.9] |
| interventions | false completions, `fail` or `unverified` (`workspace`) | 3 | 16 | 19 | 177 | 15.8% [5.5, 37.6] | 13.6% [4.7, 33.3] |
| interventions | false completions, `fail` (`empty`) | 0 | 0 | 22 | 193 | - | 0.0% [0.0, 14.9] |
| interventions | false completions, `fail` or `unverified` (`empty`) | 2 | 14 | 20 | 179 | 12.5% [3.5, 36.0] | 9.1% [2.5, 27.8] |
| setups-rerun | claim detection (vs gold `claims_tests_pass`) | 120 | 0 | 22 | 2 | 100.0% [96.9, 100.0] | 84.5% [77.7, 89.5] |
| setups-rerun | false completions, `fail` (`workspace`) | 1 | 13 | 3 | 84 | 7.1% [1.3, 31.5] | 25.0% [4.6, 69.9] |
| setups-rerun | false completions, `fail` or `unverified` (`workspace`) | 1 | 36 | 3 | 61 | 2.7% [0.5, 13.8] | 25.0% [4.6, 69.9] |
| setups-rerun | false completions, `fail` (`empty`) | 1 | 8 | 3 | 89 | 11.1% [2.0, 43.5] | 25.0% [4.6, 69.9] |
| setups-rerun | false completions, `fail` or `unverified` (`empty`) | 1 | 32 | 3 | 65 | 3.0% [0.5, 15.3] | 25.0% [4.6, 69.9] |

- `setups`, `workspace`: 184 trials; results: fail 10, pass 163, unverified 11.
- `setups`, `empty`: 184 trials; results: fail 3, pass 170, unverified 11.
- `interventions`, `workspace`: 288 trials; results: fail 4, pass 256, unverified 28.
- `interventions`, `empty`: 288 trials; results: fail 1, pass 261, unverified 26.
- `setups-rerun`, `workspace`: 144 trials; results: fail 15, pass 94, unverified 35.
- `setups-rerun`, `empty`: 144 trials; results: fail 9, pass 100, unverified 35.
