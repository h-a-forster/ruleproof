"""Score the ``claims`` check offline against labelled agent runs from overclaim.

    uv run python bench/claims_eval.py --data ../overclaim/results
    uv run python bench/claims_eval.py --data ../overclaim/results --write

overclaim (https://github.com/h-a-forster/overclaim) publishes Claude Code transcripts of
coding tasks with a hidden-check result per trial and an adjudicated label of what the final
message claims. Its ``results/<study>/`` directories hold ``gold.jsonl``,
``trials-derived.jsonl`` and, once ``transcripts.tar.gz`` is extracted, ``trials/``. This
script runs the ``claims`` check in-process on every transcript, with the rules overclaim's
own ``ruleproof`` detector uses, and reports two scores per study:

- **Claim detection.** Does ``find_claims`` find a tests, lint, types or build claim in the
  final message exactly when overclaim's gold ``claims_tests_pass`` label says the message
  asserts that tests or checks passed? This scores the phrase matcher and its hedge filters.
- **False completions.** Among trials whose gold label is ``complete``, does the check fail
  exactly when the hidden check failed? The check verifies claims against commands, so it
  cannot catch a run whose tests passed but whose code is wrong; its recall here is bounded
  by the share of false completions that had no passing run after the last edit.

The check runs in two configurations. ``workspace`` treats the agent's working directory as
the repository, as ``ruleproof check`` run in the repo does, so edits reset the evidence.
``empty`` reproduces overclaim's detector (``--repo`` an empty directory): every edit lies
outside the repository, so only the final message is read and every command counts. The
``empty`` false-completion row is the one overclaim publishes.

Study 1 (``setups``) was used while tuning the phrase matcher; study 2 (``interventions``) is
held out and was scored only after tuning. ``--write`` saves the aggregate numbers (no
transcript text) to ``bench/results/claims-eval/``. No model is called.

Standard library only, plus ruleproof itself.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruleproof import __version__
from ruleproof.checks import load_all
from ruleproof.checks.claims import find_claims
from ruleproof.errors import TranscriptError
from ruleproof.models import Context, Rule
from ruleproof.transcripts import load_session

BENCH = Path(__file__).resolve().parent
OUT = BENCH / "results" / "claims-eval"
# The rules overclaim's detector passes to `ruleproof check` (overclaim/detectors.py).
DETECTOR_CLAIMS = ["tests", "works", "build"]
# What overclaim's `claims_tests_pass` covers: tests or checks (lint, type check, build).
CHECK_CLAIMS = ["tests", "lint", "types", "build"]
DEV, HELD_OUT = "setups", "interventions"
CONFIGS = ("workspace", "empty")


@dataclass(slots=True)
class Trial:
    id: str
    study: str
    passed: bool  # hidden check
    label: str  # gold claim label: complete, partial, incomplete, none
    claims_tests_pass: bool  # gold: the final message asserts tests or checks passed
    transcript: Path


@dataclass(slots=True)
class Scored:
    trial: Trial
    status: str  # the claims check's result: pass, fail, unverified, skip
    detected: bool  # find_claims found a CHECK_CLAIMS claim in the final message
    sentence: str = ""


@dataclass(slots=True)
class Confusion:
    tp: list[str] = field(default_factory=list)
    fp: list[str] = field(default_factory=list)
    fn: list[str] = field(default_factory=list)
    tn: list[str] = field(default_factory=list)

    def add(self, trial_id: str, predicted: bool, actual: bool) -> None:
        bucket = (self.tp if actual else self.fp) if predicted else (self.fn if actual else self.tn)
        bucket.append(trial_id)

    def row(self) -> dict[str, Any]:
        tp, fp, fn, tn = len(self.tp), len(self.fp), len(self.fn), len(self.tn)
        return {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "precision": _ratio(tp, tp + fp),
            "recall": _ratio(tp, tp + fn),
            "precision_ci": wilson(tp, tp + fp),
            "recall_ci": wilson(tp, tp + fn),
        }


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for k successes in n trials (trials treated as independent)."""
    if n == 0:
        return None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def load_study(root: Path) -> list[Trial]:
    """Trials of one overclaim study directory that have a gold label and a transcript."""
    gold = {row["id"]: row for row in _jsonl(root / "gold.jsonl")}
    trials: list[Trial] = []
    for row in _jsonl(root / "trials-derived.jsonl"):
        g = gold.get(row["id"])
        if g is None or row.get("passed") is None or not row.get("transcript"):
            continue
        path = root / Path(*str(row["transcript"]).replace("\\", "/").split("/"))
        if not path.is_file():
            continue
        trials.append(
            Trial(
                id=row["id"],
                study=root.name,
                passed=bool(row["passed"]),
                label=str(g["label"]),
                claims_tests_pass=bool(g.get("claims_tests_pass", False)),
                transcript=path,
            )
        )
    return trials


def detector_rule() -> Rule:
    """The ``claims`` rule overclaim's detector runs, with the schema defaults filled in."""
    spec = load_all()["claims"]
    params = {k: p.default for k, p in spec.params.items()}
    params["claims"] = list(DETECTOR_CLAIMS)
    return Rule(id="claims", check="claims", params=params)


def score(trials: Sequence[Trial]) -> dict[str, list[Scored]]:
    """Configuration name -> one ``Scored`` per trial."""
    rule = detector_rule()
    check = load_all()["claims"].fn
    out: dict[str, list[Scored]] = {name: [] for name in CONFIGS}
    with tempfile.TemporaryDirectory(prefix="ruleproof-claims-eval-") as empty:
        repos = {"workspace": None, "empty": Path(empty)}
        for t in trials:
            try:
                session = load_session(t.transcript)
            except TranscriptError:
                for name in CONFIGS:
                    out[name].append(Scored(t, "skip", False))
                continue
            final = session.final_message()
            found = find_claims(final.text, CHECK_CLAIMS) if final else {}
            sentence = next(iter(found.values()), "")
            for name in CONFIGS:
                # A None repo makes the session's working directory the repository.
                status = check(rule, Context(repo=repos[name], session=session)).status
                out[name].append(Scored(t, status, bool(found), sentence))
    return out


def summarise(scored: dict[str, list[Scored]]) -> dict[str, Any]:
    first = scored[CONFIGS[0]]
    detection = Confusion()
    for s in first:
        detection.add(s.trial.id, s.detected, s.trial.claims_tests_pass)
    errors: dict[str, list[str]] = {
        "detection_fp": sorted(detection.fp),
        "detection_fn": sorted(detection.fn),
    }
    out: dict[str, Any] = {"trials": len(first), "claim_detection": detection.row()}
    for name, rows in scored.items():
        completions, completions_u = Confusion(), Confusion()
        for s in rows:
            t = s.trial
            if t.label == "complete" and s.status != "skip":
                completions.add(t.id, s.status == "fail", not t.passed)
                completions_u.add(t.id, s.status in ("fail", "unverified"), not t.passed)
        out[name] = {
            "statuses": dict(Counter(s.status for s in rows)),
            "false_completions": completions.row(),
            "false_completions_incl_unverified": completions_u.row(),
        }
        errors[f"{name}_completion_fp"] = sorted(completions.fp)
        errors[f"{name}_completion_tp"] = sorted(completions.tp)
    out["errors"] = errors
    return out


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{100 * value:.1f}%"


def _ci(ci: Sequence[float] | None) -> str:
    return "" if ci is None else f" [{100 * ci[0]:.1f}, {100 * ci[1]:.1f}]"


def render(results: dict[str, dict[str, Any]]) -> str:
    lines = [
        f"# claims check on overclaim's labelled runs (ruleproof {__version__})",
        "",
        f"Study `{DEV}` was used for tuning; `{HELD_OUT}` is held out. Intervals are 95% Wilson,",
        "treating trials as independent (trials of one task are correlated, so they are too",
        "narrow). False completions are scored among trials whose gold label is `complete`.",
        "`workspace` checks in the agent's working directory; `empty` reproduces overclaim's",
        "detector (an empty `--repo`, so no edit is seen).",
        "",
        "| Study | Score | TP | FP | FN | TN | Precision | Recall |",
        "|---|---|---:|---:|---:|---:|---|---|",
    ]
    names = {
        "false_completions": "false completions, `fail`",
        "false_completions_incl_unverified": "false completions, `fail` or `unverified`",
    }

    def add(study: str, label: str, row: dict[str, Any]) -> None:
        lines.append(
            f"| {study} | {label} | {row['tp']} | {row['fp']} | {row['fn']} | {row['tn']} "
            f"| {_pct(row['precision'])}{_ci(row['precision_ci'])} "
            f"| {_pct(row['recall'])}{_ci(row['recall_ci'])} |"
        )

    for study, r in results.items():
        add(study, "claim detection (vs gold `claims_tests_pass`)", r["claim_detection"])
        for config in CONFIGS:
            for key, label in names.items():
                add(study, f"{label} (`{config}`)", r[config][key])
    lines.append("")
    for study, r in results.items():
        for config in CONFIGS:
            statuses = ", ".join(f"{k} {v}" for k, v in sorted(r[config]["statuses"].items()))
            lines.append(f"- `{study}`, `{config}`: {r['trials']} trials; results: {statuses}.")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--data", required=True, type=Path, help="overclaim's results/ directory")
    ap.add_argument("--study", action="append", help=f"study to score (default: {DEV}, {HELD_OUT})")
    ap.add_argument("--errors", action="store_true", help="list the misclassified trial ids")
    ap.add_argument("--write", action="store_true", help=f"save the aggregate numbers to {OUT}")
    args = ap.parse_args(argv)

    results: dict[str, dict[str, Any]] = {}
    for study in args.study or [DEV, HELD_OUT]:
        root = args.data / study
        trials = load_study(root) if (root / "gold.jsonl").is_file() else []
        if not trials:
            print(f"{root}: no labelled trials with transcripts (extract transcripts.tar.gz)")
            return 2
        results[study] = summarise(score(trials))

    report = render(results)
    sys.stdout.write(report)
    if args.errors:
        for study, r in results.items():
            for kind, ids in r["errors"].items():
                sys.stdout.write(f"\n{study} {kind} ({len(ids)}):\n")
                sys.stdout.writelines(f"  {i}\n" for i in ids)
    if args.write:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "results.md").write_text(report, encoding="utf-8")
        summary = {"ruleproof": __version__, "detector_claims": DETECTOR_CLAIMS, **results}
        (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
