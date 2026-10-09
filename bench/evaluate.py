"""Score finished benchmark trials with ``ruleproof check`` and aggregate the results.

    python bench/evaluate.py --run-id base1

For each finished trial under ``bench/workspaces/<run-id>/`` this runs

    uv run ruleproof check --repo <ws> --rules bench/rules.toml --transcript <transcript>
        --base <initial commit> --format json --fail-on never

and writes ``bench/results/<run-id>/summary.json`` and ``results.md``. Per-trial reports are
copied to ``bench/results/<run-id>/trials/`` only after absolute paths and the user name have
been replaced (``<ws>``, ``~``, ``<user>``). Raw transcripts never leave ``bench/workspaces``.

Standard library only.
"""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BENCH = Path(__file__).resolve().parent
PROJECT = BENCH.parent
ARTIFACTS = BENCH / "workspaces"
RESULTS = BENCH / "results"
RULES = BENCH / "rules.toml"
STATUSES = ("pass", "fail", "unverified", "skip")
NOT_APPLICABLE = "n/a"  # a conditional rule whose trigger never fired (reported as a pass)
_NOT_TRIGGERED = re.compile(r"^(?:not required|no verifiable claims)", re.IGNORECASE)
CLAIMS_RULE = "claims-verified"
AGENT_FLAG = {"claude": "claude-code", "codex": "codex"}
Z95 = 1.959963984540054


# --- sanitizing --------------------------------------------------------------------------------


def _path_variants(path: str) -> set[str]:
    """Spellings of ``path`` that show up in transcripts and reports on Windows and posix."""
    p = path.rstrip("\\/")
    fwd = p.replace("\\", "/")
    out = {p, fwd, p.replace("\\", "\\\\"), fwd.replace("/", "\\")}
    m = re.match(r"^([A-Za-z]):/(.*)$", fwd)
    if m:
        drive, rest = m.group(1), m.group(2)
        out |= {f"/{drive.lower()}/{rest}", f"/mnt/{drive.lower()}/{rest}"}
        out |= {v[0].swapcase() + v[1:] for v in list(out) if re.match(r"^[A-Za-z]:", v)}
    return {v for v in out if v}


@dataclass
class Sanitizer:
    """Replace absolute paths and the user name in strings, longest match first."""

    replacements: list[tuple[str, str]] = field(default_factory=list)
    user: str | None = None

    @classmethod
    def for_trial(
        cls, workspace: str | None, extra_roots: Iterable[tuple[str, str]] = ()
    ) -> Sanitizer:
        pairs: list[tuple[str, str]] = []
        if workspace:
            pairs += [(v, "<ws>") for v in _path_variants(workspace)]
        for root, label in extra_roots:
            pairs += [(v, label) for v in _path_variants(root)]
        pairs += [(v, "<tmp>") for v in _path_variants(tempfile.gettempdir())]
        pairs += [(v, "~") for v in _path_variants(str(Path.home()))]
        pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
        return cls(pairs, Path.home().name or getpass.getuser())

    def text(self, s: str) -> str:
        for old, new in self.replacements:
            if old in s:
                s = s.replace(old, new)
            elif old.lower() in s.lower():
                s = re.sub(re.escape(old), new.replace("\\", "\\\\"), s, flags=re.IGNORECASE)
        if self.user:
            s = re.sub(
                rf"(?<![A-Za-z0-9]){re.escape(self.user)}(?![A-Za-z0-9])",
                "<user>",
                s,
                flags=re.IGNORECASE,
            )
        return s

    def json(self, data: Any) -> Any:
        if isinstance(data, str):
            return self.text(data)
        if isinstance(data, list):
            return [self.json(v) for v in data]
        if isinstance(data, dict):
            return {self.text(str(k)): self.json(v) for k, v in data.items()}
        return data


# --- statistics --------------------------------------------------------------------------------


def wilson(k: int, n: int) -> tuple[float, float] | None:
    """95% Wilson score interval for ``k`` successes out of ``n``."""
    if n == 0:
        return None
    p = k / n
    denom = 1 + Z95**2 / n
    centre = (p + Z95**2 / (2 * n)) / denom
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95**2 / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def rate(k: int, n: int) -> dict[str, Any]:
    ci = wilson(k, n)
    return {
        "k": k,
        "n": n,
        "rate": k / n if n else None,
        "ci95": [round(ci[0], 4), round(ci[1], 4)] if ci else None,
    }


def fmt_rate(r: dict[str, Any]) -> str:
    if not r["n"]:
        return "n/a (n=0)"
    lo, hi = r["ci95"]
    return f"{r['rate']:.0%} [{lo:.0%}, {hi:.0%}] ({r['k']}/{r['n']})"


# --- running ruleproof -------------------------------------------------------------------------


@dataclass
class TrialRef:
    dir: Path
    meta: dict[str, Any]
    arm: str  # the arm's name in this report (may differ from the run's, see --include)

    @property
    def label(self) -> str:
        return f"{self.arm}/{self.meta['task']}/r{self.meta['rep']}"

    @property
    def rules(self) -> Path:
        return BENCH / str(self.meta.get("rules") or RULES.name)


def find_trials(run_id: str, arms: dict[str, str] | None = None) -> list[TrialRef]:
    """Finished, non-pilot trials of ``run_id``; ``arms`` maps run arm names to report names
    and, when given, selects only those arms."""
    root = ARTIFACTS / run_id
    if not root.is_dir():
        sys.exit(f"error: no trials under {root}")
    refs = []
    for meta_path in sorted(root.glob("*/*/r*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if not meta.get("complete") or str(meta.get("task", "")).startswith("pilot"):
            continue
        if arms is not None and meta["arm"] not in arms:
            continue
        label = arms[meta["arm"]] if arms is not None else meta["arm"]
        refs.append(TrialRef(meta_path.parent, meta, label))
    return refs


def parse_include(spec: str) -> tuple[str, dict[str, str]]:
    """``RUN:ARM=LABEL[,ARM=LABEL...]`` (or ``RUN:ARM``) -> (run id, {arm: label})."""
    run_id, _, arms = spec.partition(":")
    mapping: dict[str, str] = {}
    for part in filter(None, (a.strip() for a in arms.split(","))):
        arm, _, label = part.partition("=")
        mapping[arm] = label or arm
    if not run_id or not mapping:
        sys.exit(f"error: --include {spec!r}: expected RUN:ARM[=LABEL][,...]")
    return run_id, mapping


def transcript_for(ref: TrialRef) -> Path:
    # The Codex rollout carries command exit codes that the `exec --json` stream may omit.
    rollout = ref.dir / "rollout.jsonl"
    if ref.meta.get("agent") == "codex" and rollout.is_file():
        return rollout
    return ref.dir / "transcript.jsonl"


def repo_for(ref: TrialRef) -> tuple[Path, str | None]:
    """The trial workspace; the archived copy only when the original is gone."""
    original = Path(ref.meta["workspace"])
    if original.is_dir():
        return original, None
    return (
        ref.dir / "workspace",
        "original workspace missing; checked the archived copy, so transcript paths may "
        "not map onto it",
    )


def ruleproof_argv(cmd: Sequence[str], ref: TrialRef, rules: Path | None) -> list[str]:
    repo, _ = repo_for(ref)
    return [
        *cmd,
        "check",
        "--repo",
        str(repo),
        "--rules",
        str(rules or ref.rules),
        "--transcript",
        str(transcript_for(ref)),
        "--agent",
        AGENT_FLAG.get(ref.meta.get("agent", ""), "auto"),
        "--base",
        ref.meta["base_sha"],
        "--format",
        "json",
        "--fail-on",
        "never",
    ]


def check_trial(cmd: Sequence[str], ref: TrialRef, rules: Path | None) -> dict[str, Any]:
    argv = ruleproof_argv(cmd, ref, rules)
    res = subprocess.run(
        argv,
        cwd=PROJECT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        # ruleproof writes non-ASCII (e.g. quoted agent output); a Windows console codepage
        # cannot encode it.
        env=os.environ | {"PYTHONIOENCODING": "utf-8"},
    )
    (ref.dir / "ruleproof.stderr.txt").write_text(res.stderr, encoding="utf-8")
    if res.returncode not in (0, 1):
        return {"error": f"ruleproof exited {res.returncode}: {res.stderr.strip()[-500:]}"}
    try:
        report = json.loads(res.stdout)
    except ValueError as e:
        return {"error": f"ruleproof output is not JSON: {e}"}
    (ref.dir / "ruleproof.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {"report": report}


def statuses(report: dict[str, Any]) -> dict[str, str]:
    """``{rule id: status}`` from a ruleproof JSON report, tolerant of small schema changes.

    A pass whose summary says the rule was never triggered becomes ``n/a``, so conditional
    rules are rated only on the trials they applied to.
    """
    out: dict[str, str] = {}
    for item in report.get("results", []):
        if not isinstance(item, dict):
            continue
        rule = item.get("rule")
        rid = rule.get("id") if isinstance(rule, dict) else item.get("id", item.get("rule_id"))
        status = item.get("status")
        if isinstance(rid, str) and status in STATUSES:
            summary = item.get("summary")
            if status == "pass" and isinstance(summary, str) and _NOT_TRIGGERED.match(summary):
                status = NOT_APPLICABLE
            out[rid] = status
    return out


# --- aggregation -------------------------------------------------------------------------------


def aggregate(scored: list[tuple[TrialRef, dict[str, str] | None]]) -> dict[str, Any]:
    by_arm: dict[str, list[tuple[TrialRef, dict[str, str] | None]]] = defaultdict(list)
    for ref, st in scored:
        by_arm[ref.arm].append((ref, st))

    arms: dict[str, Any] = {}
    for arm, items in sorted(by_arm.items(), key=lambda kv: _arm_order(kv[0])):
        valid = [(r, s) for r, s in items if not r.meta.get("error")]
        graded = [r for r, _ in valid if r.meta.get("hidden_pass") is not None]
        success = sum(1 for r in graded if r.meta["hidden_pass"])
        by_task: dict[str, list[TrialRef]] = defaultdict(list)
        for r in graded:
            by_task[r.meta["task"]].append(r)

        checked = [(r, s) for r, s in valid if s is not None]
        rule_ids = sorted({rid for _, s in checked for rid in s})
        rules: dict[str, Any] = {}
        for rid in rule_ids:
            counts = Counter(s.get(rid, "missing") for _, s in checked)
            rules[rid] = {
                "compliance": rate(counts["pass"], counts["pass"] + counts["fail"]),
                **{st: counts[st] for st in (*STATUSES, NOT_APPLICABLE, "missing")},
            }

        claims = [s.get(CLAIMS_RULE) for _, s in checked]
        claims_fail = sum(1 for c in claims if c == "fail")
        broken = [sum(1 for v in s.values() if v == "fail") for _, s in checked]
        broken_no_claims = [
            sum(1 for k, v in s.items() if v == "fail" and k != CLAIMS_RULE) for _, s in checked
        ]
        failures = Counter(rid for _, s in checked for rid, v in s.items() if v == "fail")
        clean = sum(1 for n in broken if n == 0)
        hooked = [r for r, _ in valid if r.meta.get("hook")]
        walls = [float(r.meta["wall_s"]) for r, _ in valid if r.meta.get("wall_s") is not None]
        costs = [float(r.meta["cost_usd"]) for r, _ in valid if r.meta.get("cost_usd") is not None]
        arms[arm] = {
            "trials": len(items),
            "errored": len(items) - len(valid),
            "scored": len(checked),
            "models": sorted({str(r.meta.get("model")) for r, _ in valid}),
            "agent_versions": sorted({str(r.meta.get("agent_version")) for r, _ in valid}),
            "task_success": rate(success, len(graded)),
            "task_success_by_task": {
                t: rate(sum(1 for r in rs if r.meta["hidden_pass"]), len(rs))
                for t, rs in sorted(by_task.items())
            },
            "suite_passes_at_end": rate(
                sum(1 for r, _ in valid if r.meta.get("suite_pass")),
                sum(1 for r, _ in valid if r.meta.get("suite_pass") is not None),
            ),
            "claimed_not_verified": {
                **rate(claims_fail, len(claims)),
                "trials_with_checkable_claims": sum(1 for c in claims if c in STATUSES),
                "unverified": sum(1 for c in claims if c == "unverified"),
                "skip": sum(1 for c in claims if c == "skip"),
            },
            "all_rules_followed": rate(clean, len(broken)),
            "rules_broken_per_trial": _mean(broken),
            "rule_failures": dict(failures.most_common()),
            "hook": _hook_stats(hooked) if hooked else None,
            "rules_broken_per_trial_excl_claims": _mean(broken_no_claims),
            "rules": rules,
            "committed": sum(1 for r, _ in valid if r.meta.get("committed")),
            "timed_out": sum(1 for r, _ in valid if r.meta.get("timed_out")),
            "wall_s": _mean(walls),
            "cost_usd_total": round(sum(costs), 4) if costs else None,
            "cost_usd_mean": round(statistics.fmean(costs), 4) if costs else None,
        }
    return arms


def _arm_order(name: str) -> tuple[int, int, int, str]:
    """Sonnet, Haiku 4.5, Haiku 5.5; short before long; no hook before hook; others last."""
    model = next((i for i, m in enumerate(("sonnet", "haiku45", "haiku55")) if m in name), 3)
    return (model, int("long" in name), int(name.endswith("-hook")), name)


def _hook_stats(trials: list[TrialRef]) -> dict[str, Any]:
    blocks = [int(r.meta.get("hook_blocks") or 0) for r in trials]
    denies = [int(r.meta.get("pretool_denies") or 0) for r in trials]
    rules = Counter(rid for r in trials for rid in r.meta.get("hook_blocked_rules") or [])
    denied = Counter(rid for r in trials for rid in r.meta.get("pretool_denied_rules") or [])
    return {
        "trials": len(trials),
        "runs": sum(int(r.meta.get("hook_runs") or 0) for r in trials),
        "blocked_at_least_once": rate(sum(1 for b in blocks if b), len(blocks)),
        "blocks_per_trial": _mean(blocks),
        "blocked_rules": dict(rules.most_common()),
        "pretool_runs": sum(int(r.meta.get("pretool_runs") or 0) for r in trials),
        "denied_at_least_once": rate(sum(1 for d in denies if d), len(denies)),
        "denies_per_trial": _mean(denies),
        "denied_rules": dict(denied.most_common()),
        "hook_failures": sum(int(r.meta.get("hook_failures") or 0) for r in trials),
    }


def _mean(xs: list[int] | list[float]) -> dict[str, Any]:
    if not xs:
        return {"mean": None, "sd": None, "n": 0}
    sd = statistics.stdev(xs) if len(xs) > 1 else 0.0
    return {"mean": round(statistics.fmean(xs), 3), "sd": round(sd, 3), "n": len(xs)}


def render_markdown(
    run_id: str, arms: dict[str, Any], rule_info: dict[str, str], sources: list[str]
) -> str:
    names = list(arms)
    lines = [f"# Benchmark results: `{run_id}`", ""]
    if sources:
        lines += ["Trials: " + "; ".join(sources) + ".", ""]
    lines += [
        "Rates are k/n with 95% Wilson intervals. Rule compliance is pass / (pass + fail); "
        "`unverified` (evidence exists but cannot be confirmed, e.g. no exit code), `skip` "
        "(input missing) and `n/a` (a conditional rule that never triggered, e.g. codegen when "
        "the spec did not change) are counted separately and excluded from the rate. "
        '"Claimed but not verified" is over all scored trials: a trial counts when the '
        "agent's final report claims a result (tests pass, committed, ...) with no successful "
        "matching command after its last edit.",
        "",
        "## Headline",
        "",
        "| arm | n | task success | all rules followed | rules broken per trial | "
        "claimed but not verified | hooks fired |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in names:
        a = arms[name]
        hook = a["hook"]
        hook_cell = (
            "-"
            if hook is None
            else f"denied {hook['denied_at_least_once']['k']}/{hook['trials']}, "
            f"blocked {hook['blocked_at_least_once']['k']}/{hook['trials']}"
        )
        lines.append(
            f"| {name} | {a['scored']} | {fmt_rate(a['task_success'])} | "
            f"{fmt_rate(a['all_rules_followed'])} | {_fmt_mean(a['rules_broken_per_trial'])} | "
            f"{fmt_rate(a['claimed_not_verified'])} | {hook_cell} |"
        )
    lines += [
        "",
        "Hook arms register two ruleproof hooks: PreToolUse (prevention: denies a forbidden "
        "command, edit or tool call before it runs) and Stop (repair: blocks finishing while "
        "rules fail). Every number describes the final state, after any fixes the hooks "
        'prompted. "Hooks fired" counts trials with at least one PreToolUse denial and '
        "trials with at least one Stop block.",
        "",
        "## Overview",
        "",
        "| | " + " | ".join(names) + " |",
        "| --- |" + " --- |" * len(names),
    ]

    def row(label: str, cell: Any) -> None:
        lines.append(f"| {label} | " + " | ".join(cell(arms[a]) for a in names) + " |")

    row("trials (errored)", lambda a: f"{a['trials']} ({a['errored']})")
    row("models", lambda a: ", ".join(a["models"]))
    row("task success (hidden tests)", lambda a: fmt_rate(a["task_success"]))
    row("own suite passes at end", lambda a: fmt_rate(a["suite_passes_at_end"]))
    row("claimed but not verified", lambda a: fmt_rate(a["claimed_not_verified"]))
    row(
        "rules broken per trial, mean (sd)",
        lambda a: _fmt_mean(a["rules_broken_per_trial"]),
    )
    row("committed despite the rule", lambda a: str(a["committed"]))
    row(
        "hooks: PreToolUse denies / Stop blocks per trial; failures",
        lambda a: (
            "-"
            if a["hook"] is None
            else f"{_fmt_mean(a['hook']['denies_per_trial'])} / "
            f"{_fmt_mean(a['hook']['blocks_per_trial'])}; {a['hook']['hook_failures']}"
        ),
    )
    row("timed out", lambda a: str(a["timed_out"]))
    row("wall time per trial, s", lambda a: _fmt_mean(a["wall_s"]))
    row("cost, USD total (mean)", lambda a: _fmt_cost(a))

    lines += ["", "## Rule compliance", "", "| rule | " + " | ".join(names) + " |"]
    lines.append("| --- |" + " --- |" * len(names))
    rule_ids = sorted({rid for a in arms.values() for rid in a["rules"]})
    for rid in rule_ids:
        cells = []
        for a in names:
            r = arms[a]["rules"].get(rid)
            if r is None:
                cells.append("-")
                continue
            extra = ", ".join(
                f"{st} {r[st]}" for st in ("unverified", "skip", NOT_APPLICABLE) if r[st]
            )
            cells.append(fmt_rate(r["compliance"]) + (f"; {extra}" if extra else ""))
        src = rule_info.get(rid, "")
        lines.append(f"| `{rid}`{f' ({src})' if src else ''} | " + " | ".join(cells) + " |")

    lines += ["", "## Rules broken most often", "", "Failing trials per rule.", ""]
    lines.append("| rule | " + " | ".join(names) + " | total |")
    lines.append("| --- |" + " --- |" * (len(names) + 1))
    totals = Counter[str]()
    for a in arms.values():
        totals.update(a["rule_failures"])
    for rid, total in totals.most_common():
        cells = [str(arms[a]["rule_failures"].get(rid, 0)) for a in names]
        lines.append(f"| `{rid}` | " + " | ".join(cells) + f" | {total} |")
    if not totals:
        lines.append("| (none) |" + " 0 |" * (len(names) + 1))

    hooked = [a for a in names if arms[a]["hook"] is not None]
    if hooked:
        lines += [
            "",
            "## Hooks",
            "",
            "| arm | trials | PreToolUse: denied at least once | rules denied | "
            "Stop: blocked at least once | rules blocked on | hook failures |",
        ]
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for a in hooked:
            h = arms[a]["hook"]
            blocked = ", ".join(f"`{k}` {v}" for k, v in h["blocked_rules"].items()) or "-"
            denied = ", ".join(f"`{k}` {v}" for k, v in h["denied_rules"].items()) or "-"
            lines.append(
                f"| {a} | {h['trials']} | {fmt_rate(h['denied_at_least_once'])} | {denied} | "
                f"{fmt_rate(h['blocked_at_least_once'])} | {blocked} | {h['hook_failures']} |"
            )

    lines += ["", "## Task success by task", "", "| task | " + " | ".join(names) + " |"]
    lines.append("| --- |" + " --- |" * len(names))
    tasks = sorted({t for a in arms.values() for t in a["task_success_by_task"]})
    for t in tasks:
        cells = [
            fmt_rate(arms[a]["task_success_by_task"][t])
            if t in arms[a]["task_success_by_task"]
            else "-"
            for a in names
        ]
        lines.append(f"| {t} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _fmt_mean(m: dict[str, Any]) -> str:
    return "n/a" if m["mean"] is None else f"{m['mean']:.2f} ({m['sd']:.2f}), n={m['n']}"


def _fmt_cost(a: dict[str, Any]) -> str:
    if a["cost_usd_total"] is None:
        return "not reported"
    return f"{a['cost_usd_total']:.2f} ({a['cost_usd_mean']:.2f})"


def rule_sources(files: Iterable[Path]) -> dict[str, str]:
    """``{rule id: "AGENTS.md:9 (rules.toml), AGENTS.md:212 (rules.long.toml)"}``."""
    import tomllib

    out: dict[str, list[str]] = defaultdict(list)
    for path in files:
        with path.open("rb") as f:
            data = tomllib.load(f)
        for r in data.get("rule", []):
            if isinstance(r, dict) and "id" in r and r.get("source"):
                out[r["id"]].append(f"{r['source']} in `{path.name}`")
    return {k: ", ".join(v) for k, v in out.items()}


# --- CLI ---------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    p.add_argument("--run-id", required=True, help="the run to score; results go to its name")
    p.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="RUN:ARM[=LABEL]",
        help="also score these arms of another run, optionally renamed (repeatable)",
    )
    p.add_argument(
        "--rules",
        type=Path,
        help="rules file for every trial (default: the one each trial recorded)",
    )
    p.add_argument(
        "--ruleproof",
        default="uv run ruleproof",
        help="command that runs ruleproof (default: %(default)s)",
    )
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument(
        "--no-check",
        action="store_true",
        help="aggregate existing ruleproof.json reports without re-running ruleproof",
    )
    args = p.parse_args(argv)

    refs = find_trials(args.run_id)
    sources = [f"`{args.run_id}` (all arms)"]
    for spec in args.include:
        run_id, mapping = parse_include(spec)
        refs += find_trials(run_id, mapping)
        named = ", ".join(f"{k} as {v}" if k != v else k for k, v in mapping.items())
        sources.append(f"`{run_id}` ({named})")
    cmd = args.ruleproof.split()
    exe = shutil.which(cmd[0])
    if exe is None:
        sys.exit(f"error: {cmd[0]!r} not found on PATH")
    cmd[0] = exe

    def score(ref: TrialRef) -> tuple[TrialRef, dict[str, Any]]:
        if ref.meta.get("error"):
            return ref, {"error": f"trial errored: {ref.meta['error']}"}
        if args.no_check:
            path = ref.dir / "ruleproof.json"
            if not path.is_file():
                return ref, {"error": "no ruleproof.json"}
            return ref, {"report": json.loads(path.read_text(encoding="utf-8"))}
        return ref, check_trial(cmd, ref, args.rules)

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        outcomes = list(pool.map(score, refs))

    out_dir = RESULTS / args.run_id
    trials_dir = out_dir / "trials"
    scored: list[tuple[TrialRef, dict[str, str] | None]] = []
    problems = []
    for ref, outcome in outcomes:
        report = outcome.get("report")
        if report is None:
            problems.append(f"{ref.label}: {outcome['error']}")
            scored.append((ref, None))
            continue
        scored.append((ref, statuses(report)))
        repo, note = repo_for(ref)
        san = Sanitizer.for_trial(
            ref.meta.get("workspace"), [(str(repo), "<ws>"), (str(BENCH), "<bench>")]
        )
        clean = san.json(
            {"arm": ref.arm, "trial": _public_meta(ref.meta), "note": note, "report": report}
        )
        dest = trials_dir / ref.arm / ref.meta["task"] / f"r{ref.meta['rep']}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    arms = aggregate(scored)
    summary = {
        "run_id": args.run_id,
        "sources": sources,
        "rules": sorted({ref.rules.name for ref in refs})
        if args.rules is None
        else [args.rules.name],
        "arms": arms,
        "problems": problems,
    }
    san = Sanitizer.for_trial(None, [(str(BENCH), "<bench>")])
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(san.json(summary), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out_dir / "results.md").write_text(
        san.text(
            render_markdown(
                args.run_id,
                arms,
                rule_sources([args.rules] if args.rules else sorted({r.rules for r in refs})),
                sources,
            )
        ),
        encoding="utf-8",
    )
    for line in problems:
        print(f"warning: {line}", file=sys.stderr)
    print(f"wrote {out_dir / 'summary.json'} and results.md ({len(refs)} trials)")
    return 0


PUBLIC_META = (
    "run_id",
    "arm",
    "agent",
    "task",
    "rep",
    "agent_version",
    "model",
    "models_used",
    "exit_code",
    "timed_out",
    "wall_s",
    "cost_usd",
    "num_turns",
    "result_subtype",
    "hidden_pass",
    "suite_pass",
    "committed",
    "base_sha",
    "codex_home",
    "model_alias",
    "variant",
    "rules",
    "hook",
    "hook_runs",
    "hook_blocks",
    "hook_failures",
    "hook_blocked_rules",
    "pretool_runs",
    "pretool_denies",
    "pretool_denied_rules",
    "arm_renamed_from",
    "ruleproof_commit",
    "error",
)


def _public_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {k: meta.get(k) for k in PUBLIC_META}


if __name__ == "__main__":
    raise SystemExit(main())
