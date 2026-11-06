# Backlog

## In progress

## Waiting on me

- `polish-2026-10-30` is merged to `main` and tagged `v0.1.1`; the install pins in the README and docs already point at the tag.
- CHANGELOG `[0.1.1]` lists the late fixes from `docs/release-notes-0.1.1.md`, and CHANGELOG and CITATION.cff carry the tag date (the release body comes from the CHANGELOG, which agents do not edit).
- Dependabot PR #6 (pytest 9.2.0, ruff 0.17.3, mypy 2.4.1) is reviewed and merged or closed; its CI is green.
- Merged remote branches (`claude/*`, `upgrades-2026-10-23`, the old Dependabot branch) are deleted.

## Decided

- 2026-11-06: workflows and `action.yml` pin actions to commit SHAs; README, docs and `examples/` keep version tags, because Dependabot does not update them and people copy them.
- 2026-11-06: the rerun's numbers stay out of the default `claims_eval.py` studies until overclaim uploads its transcripts.

## Proposed

- It is decided whether CHANGELOG stays hand-kept (AGENTS.md, the `changelog` rule, `release.yml`) or `docs/release-notes-<version>.md` replaces it.
- Edits to git-ignored scratch paths no longer reset the `claims` evidence, which removes most remaining false failures on overclaim's runs.
- A command line that tests the fix and then the old code to show the tests fail is read by its first run, not as a failure.
- Claims evidence that a test ran survives a run the agent backgrounds (`pt1` failure list).
- The pressure benchmark is rerun with transcripts kept, so `claims` fixes can be scored out of sample.
- Gemini CLI parsing is tested on real sessions.
- `setup-uv` moves from v7.6.0 to the current major after a diff of what changed.
