# claims check on overclaim's labelled runs (ruleproof 0.1.0)

Study `setups` was used for tuning; `interventions` is held out. Intervals are 95% Wilson,
treating trials as independent (trials of one task are correlated, so they are too
narrow). False completions are scored among trials whose gold label is `complete`.
`workspace` checks in the agent's working directory; `empty` reproduces overclaim's
detector (an empty `--repo`, so no edit is seen).

| Study | Score | TP | FP | FN | TN | Precision | Recall |
|---|---|---:|---:|---:|---:|---|---|
| setups | claim detection (vs gold `claims_tests_pass`) | 137 | 5 | 7 | 35 | 96.5% [92.0, 98.5] | 95.1% [90.3, 97.6] |
| setups | false completions, `fail` (`workspace`) | 0 | 3 | 11 | 84 | 0.0% [0.0, 56.2] | 0.0% [0.0, 25.9] |
| setups | false completions, `fail` or `unverified` (`workspace`) | 1 | 12 | 10 | 75 | 7.7% [1.4, 33.3] | 9.1% [1.6, 37.7] |
| setups | false completions, `fail` (`empty`) | 0 | 0 | 11 | 87 | - | 0.0% [0.0, 25.9] |
| setups | false completions, `fail` or `unverified` (`empty`) | 1 | 9 | 10 | 78 | 10.0% [1.8, 40.4] | 9.1% [1.6, 37.7] |
| interventions | claim detection (vs gold `claims_tests_pass`) | 252 | 2 | 31 | 3 | 99.2% [97.2, 99.8] | 89.0% [84.9, 92.2] |
| interventions | false completions, `fail` (`workspace`) | 0 | 1 | 22 | 192 | 0.0% [0.0, 79.3] | 0.0% [0.0, 14.9] |
| interventions | false completions, `fail` or `unverified` (`workspace`) | 3 | 16 | 19 | 177 | 15.8% [5.5, 37.6] | 13.6% [4.7, 33.3] |
| interventions | false completions, `fail` (`empty`) | 0 | 0 | 22 | 193 | - | 0.0% [0.0, 14.9] |
| interventions | false completions, `fail` or `unverified` (`empty`) | 2 | 14 | 20 | 179 | 12.5% [3.5, 36.0] | 9.1% [2.5, 27.8] |

- `setups`, `workspace`: 184 trials; results: fail 11, pass 162, unverified 11.
- `setups`, `empty`: 184 trials; results: fail 3, pass 170, unverified 11.
- `interventions`, `workspace`: 288 trials; results: fail 5, pass 256, unverified 27.
- `interventions`, `empty`: 288 trials; results: fail 1, pass 261, unverified 26.
