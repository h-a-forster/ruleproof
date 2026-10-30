# claims check on overclaim's labelled runs (ruleproof at e33dc0f, the README's "before")

Reproduce: put a `git worktree` of `e33dc0f`'s `src` first on `PYTHONPATH` and run
`bench/claims_eval.py` from the current tree (see [bench/README.md](../../README.md)).

Study `setups` was used for tuning; `interventions` is held out. Intervals are 95% Wilson,
treating trials as independent (trials of one task are correlated, so they are too
narrow). False completions are scored among trials whose gold label is `complete`.
`workspace` checks in the agent's working directory; `empty` reproduces overclaim's
detector (an empty `--repo`, so no edit is seen).

| Study | Score | TP | FP | FN | TN | Precision | Recall |
|---|---|---:|---:|---:|---:|---|---|
| setups | claim detection (vs gold `claims_tests_pass`) | 118 | 6 | 26 | 34 | 95.2% [89.8, 97.8] | 81.9% [74.9, 87.4] |
| setups | false completions, `fail` (`workspace`) | 0 | 9 | 11 | 78 | 0.0% [0.0, 29.9] | 0.0% [0.0, 25.9] |
| setups | false completions, `fail` or `unverified` (`workspace`) | 7 | 45 | 4 | 42 | 13.5% [6.7, 25.3] | 63.6% [35.4, 84.8] |
| setups | false completions, `fail` (`empty`) | 0 | 7 | 11 | 80 | 0.0% [0.0, 35.4] | 0.0% [0.0, 25.9] |
| setups | false completions, `fail` or `unverified` (`empty`) | 7 | 42 | 4 | 45 | 14.3% [7.1, 26.7] | 63.6% [35.4, 84.8] |
| interventions | claim detection (vs gold `claims_tests_pass`) | 235 | 2 | 48 | 3 | 99.2% [97.0, 99.8] | 83.0% [78.2, 87.0] |
| interventions | false completions, `fail` (`workspace`) | 0 | 2 | 22 | 191 | 0.0% [0.0, 65.8] | 0.0% [0.0, 14.9] |
| interventions | false completions, `fail` or `unverified` (`workspace`) | 13 | 120 | 9 | 73 | 9.8% [5.8, 16.0] | 59.1% [38.7, 76.7] |
| interventions | false completions, `fail` (`empty`) | 0 | 1 | 22 | 192 | 0.0% [0.0, 79.3] | 0.0% [0.0, 14.9] |
| interventions | false completions, `fail` or `unverified` (`empty`) | 12 | 118 | 10 | 75 | 9.2% [5.4, 15.4] | 54.5% [34.7, 73.1] |

- `setups`, `workspace`: 184 trials; results: fail 17, pass 109, unverified 58.
- `setups`, `empty`: 184 trials; results: fail 12, pass 116, unverified 56.
- `interventions`, `workspace`: 288 trials; results: fail 4, pass 101, unverified 183.
- `interventions`, `empty`: 288 trials; results: fail 1, pass 113, unverified 174.
