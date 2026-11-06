# claims check on overclaim's labelled runs (ruleproof 0.1.1)

Study `setups` was used for tuning; `interventions` is held out. Intervals are 95% Wilson,
treating trials as independent (trials of one task are correlated, so they are too
narrow). False completions are scored among trials whose gold label is `complete`.
`workspace` checks in the agent's working directory; `empty` reproduces overclaim's
detector (an empty `--repo`, so no edit is seen).

| Study | Score | TP | FP | FN | TN | Precision | Recall |
|---|---|---:|---:|---:|---:|---|---|
| setups-rerun | claim detection (vs gold `claims_tests_pass`) | 120 | 0 | 22 | 2 | 100.0% [96.9, 100.0] | 84.5% [77.7, 89.5] |
| setups-rerun | false completions, `fail` (`workspace`) | 2 | 22 | 2 | 75 | 8.3% [2.3, 25.8] | 50.0% [15.0, 85.0] |
| setups-rerun | false completions, `fail` or `unverified` (`workspace`) | 2 | 42 | 2 | 55 | 4.5% [1.3, 15.1] | 50.0% [15.0, 85.0] |
| setups-rerun | false completions, `fail` (`empty`) | 1 | 6 | 3 | 91 | 14.3% [2.6, 51.3] | 25.0% [4.6, 69.9] |
| setups-rerun | false completions, `fail` or `unverified` (`empty`) | 2 | 35 | 2 | 62 | 5.4% [1.5, 17.7] | 50.0% [15.0, 85.0] |

- `setups-rerun`, `workspace`: 144 trials; results: fail 28, pass 84, unverified 32.
- `setups-rerun`, `empty`: 144 trials; results: fail 7, pass 93, unverified 44.
