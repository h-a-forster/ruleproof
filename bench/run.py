"""Run coding agents on the benchmark tasks and record what they did.

Each trial copies ``bench/template`` into a fresh git repository under the system temp dir
(outside any directory whose instruction files an agent could pick up), runs one agent
headless with the task prompt on stdin, then saves the transcript, the diff, and the result of
the hidden test. Artifacts go to ``bench/workspaces/<run-id>/<arm>/<task>/r<rep>/``.

    python bench/run.py --arms claude-sonnet,codex --tasks all --reps 3 --jobs 3 --run-id base1
    python bench/run.py --pilot --arms claude-sonnet,codex --run-id pilot1

Standard library only. Re-running with the same ``--run-id`` skips finished trials.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BENCH = Path(__file__).resolve().parent
TEMPLATE = BENCH / "template"
TASKS = BENCH / "tasks"
PILOT_PROMPT = BENCH / "pilot" / "prompt.md"
VARIANTS: dict[str, tuple[Path | None, Path]] = {
    # name: (directory whose files replace the template's, rules file for that variant)
    "short": (None, BENCH / "rules.toml"),
    "long": (BENCH / "variants" / "long", BENCH / "rules.long.toml"),
}
# Words that open each rule's prose in AGENTS.md; checked against every rules file's `source`.
SOURCE_ANCHORS: dict[str, str] = {
    "tests-pass": "Tests live in `tests/`",
    "claims-verified": "Tests live in `tests/`",
    "no-pip-install": "We use uv for everything",
    "no-new-dependencies": "Please don't add dependencies",
    "no-uv-add": "Please don't add dependencies",
    "generated-not-hand-edited": "The tables in `src/invoicing/generated/`",
    "generated-changes-with-spec": "The tables in `src/invoicing/generated/`",
    "codegen-after-spec-change": "The tables in `src/invoicing/generated/`",
    "no-print-in-src": "Library code logs through",
    "no-editing-existing-tests": "Don't modify existing test files",
    "changelog-entry": "Every change under `src/`",
    "no-commit": "Don't commit.",
    "no-backup-files": "Don't commit.",
}
ARTIFACTS = BENCH / "workspaces"
WORK_ROOT = Path(tempfile.gettempdir()) / "ruleproof-bench"

TRIAL_TIMEOUT_S = 20 * 60
GRADE_TIMEOUT_S = 5 * 60
CLAUDE_BUDGET_USD = "1.00"

INSTRUCTION_FILES = (
    "AGENTS.md",
    "AGENTS.override.md",
    "CLAUDE.md",
    "CLAUDE.local.md",
    "GEMINI.md",
    ".claude/CLAUDE.md",
)
CACHE_DIRS = (".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")
COPY_IGNORE = shutil.ignore_patterns(*CACHE_DIRS)
# Variables a parent agent session (e.g. Claude Code running this script) leaks into children.
SCRUB_ENV = re.compile(r"^(?:CLAUDE|CODEX_(?!HOME$)|GIT_(?:DIR|WORK_TREE|INDEX_FILE)$)", re.I)

_print_lock = threading.Lock()


@dataclass(frozen=True)
class Arm:
    name: str
    agent: str  # "claude" | "codex"
    model: str | None = None  # Claude Code model alias; None = the agent's default
    variant: str = "short"
    hook: bool = False  # Claude Code Stop hook running `ruleproof hook claude-stop`

    @property
    def rules(self) -> Path:
        return VARIANTS[self.variant][1]


SONNET = "claude-sonnet-5-5"
HAIKU45 = "claude-haiku-4-5-20251001"
HAIKU55 = "claude-haiku-5-5"

ARMS: dict[str, Arm] = {
    a.name: a
    for a in (
        # Run base1 used the alias `sonnet` (it resolved to claude-sonnet-5-5); report it as
        # sonnet-short.
        Arm("claude-sonnet", "claude", "sonnet"),
        Arm("sonnet-short", "claude", SONNET),
        Arm("sonnet-long", "claude", SONNET, "long"),
        Arm("sonnet-long-hook", "claude", SONNET, "long", hook=True),
        # Run p2's haiku45 arms used the alias `haiku`, which resolved to HAIKU45.
        Arm("haiku45-short", "claude", HAIKU45),
        Arm("haiku45-long", "claude", HAIKU45, "long"),
        Arm("haiku45-long-hook", "claude", HAIKU45, "long", hook=True),
        Arm("haiku45-short-hook", "claude", HAIKU45, hook=True),  # hook pilot only
        Arm("haiku55-short", "claude", HAIKU55),
        Arm("haiku55-long", "claude", HAIKU55, "long"),
        Arm("haiku55-long-hook", "claude", HAIKU55, "long", hook=True),
        Arm("codex", "codex"),
    )
}


@dataclass(frozen=True)
class Task:
    name: str
    prompt: str
    hidden_test: Path | None


@dataclass(frozen=True)
class Trial:
    run_id: str
    arm: Arm
    task: Task
    rep: int
    codex_home: Path | None = None  # a separate CODEX_HOME, so ~/.codex/AGENTS.md stays out
    ruleproof: FrozenRuleproof | None = None  # the build the Stop hook runs (hook arms only)

    @property
    def key(self) -> str:
        return f"{self.arm.name}/{self.task.name}/r{self.rep}"

    @property
    def out(self) -> Path:
        return ARTIFACTS / self.run_id / self.arm.name / self.task.name / f"r{self.rep}"

    @property
    def ws(self) -> Path:
        return WORK_ROOT / self.run_id / self.arm.name / self.task.name / f"r{self.rep}" / "ws"


def log(msg: str) -> None:
    with _print_lock:
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- environment and isolation ---------------------------------------------------------------


def child_env(codex_home: Path | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not SCRUB_ENV.match(k)}
    venv = env.pop("VIRTUAL_ENV", None)
    if venv:
        # Launched via `uv run`: keep this project's venv off the agents' PATH.
        root = os.path.normcase(os.path.abspath(venv))
        parts = env.get("PATH", "").split(os.pathsep)
        env["PATH"] = os.pathsep.join(
            p for p in parts if not os.path.normcase(os.path.abspath(p or ".")).startswith(root)
        )
    env["NO_COLOR"] = "1"
    env["UV_NO_PROGRESS"] = "1"
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    return env


def instruction_files_above(path: Path) -> list[Path]:
    """Instruction files in the ancestors of ``path`` that an agent might load.

    ``~/.claude/CLAUDE.md`` is the user-level file; the agent flags are what keep it out, and
    the pilot checks that, so it is not reported here.
    """
    home_user_file = (Path.home() / ".claude" / "CLAUDE.md").resolve()
    found = []
    for parent in path.resolve().parents:
        for name in INSTRUCTION_FILES:
            candidate = parent / name
            if candidate.is_file() and candidate.resolve() != home_user_file:
                found.append(candidate)
    return found


def resolve_exe(name: str) -> str:
    exe = shutil.which(name)
    if exe is None:
        sys.exit(f"error: {name!r} not found on PATH")
    return exe


def agent_argv(arm: Arm, ws: Path) -> list[str]:
    if arm.agent == "claude":
        argv = [
            resolve_exe("claude"),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            arm.model or "sonnet",
            "--permission-mode",
            "bypassPermissions",
            "--max-budget-usd",
            CLAUDE_BUDGET_USD,
            # User settings carry hooks, plugins and permissions; project + local keep the
            # repo's own settings (the hook arm needs them).
            "--setting-sources",
            "project,local",
            "--strict-mcp-config",
            "--settings",
            json.dumps({"claudeMdExcludes": [user_claude_md()]}),
        ]
        if arm.hook:
            argv.append("--include-hook-events")
        return argv
    return [
        resolve_exe("codex"),
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        "skills.include_instructions=false",
        "-C",
        str(ws),
        "-",
    ]


def user_claude_md() -> str:
    return (Path.home() / ".claude" / "CLAUDE.md").as_posix()


@dataclass(frozen=True)
class FrozenRuleproof:
    """A checkout of one ruleproof commit with its own venv, so hook runs never see
    half-edited code from the working tree."""

    commit: str
    root: Path


def freeze_ruleproof(ref: str) -> FrozenRuleproof:
    project = BENCH.parent
    commit = git(project, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    root = WORK_ROOT / f"ruleproof-{commit[:12]}"
    if not (root / ".git").exists():
        rmtree(root)
        git(project, "worktree", "add", "--detach", str(root), commit)
    head = git(root, "rev-parse", "HEAD").strip()
    if head != commit or git(root, "status", "--porcelain", "--untracked-files=no").strip():
        sys.exit(f"error: {root} is not a clean checkout of {commit}")
    uv = resolve_exe("uv")
    subprocess.run(
        [uv, "sync", "--frozen", "--project", str(root)],
        check=True,
        capture_output=True,
        env=child_env(),
    )
    for spec in HOOKS.values():
        res = subprocess.run(
            [
                uv,
                "run",
                "--no-sync",
                "--project",
                str(root),
                "ruleproof",
                "hook",
                spec.name,
                "--help",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env(),
        )
        needed = ["--rules", "--base"] if spec.base else ["--rules"]
        if res.returncode != 0 or any(opt not in res.stdout for opt in needed):
            sys.exit(f"error: ruleproof {commit[:12]} has no `hook {spec.name} {' '.join(needed)}`")
    return FrozenRuleproof(commit, root)


# Claude Code hook event -> ruleproof hook. PreToolUse prevents (denies a forbidden command,
# edit or tool before it runs); Stop repairs (blocks finishing while rules fail).
@dataclass(frozen=True)
class HookSpec:
    name: str  # `ruleproof hook <name>`
    matcher: str | None  # PreToolUse tool-name matcher; Stop hooks take none
    base: bool  # pass --base <initial sha>
    timeout_s: int  # generous: `uv run` adds startup time on every call


HOOKS = {
    "PreToolUse": HookSpec(
        "claude-pretool", "Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit", True, 30
    ),
    "Stop": HookSpec("claude-stop", None, True, 120),
}


def hook_command(ruleproof: FrozenRuleproof, spec: HookSpec, rules: Path, base: str) -> str:
    # --no-sync: trials run in parallel and must not re-sync the shared venv.
    cmd = (
        f'uv run --no-sync --project "{ruleproof.root.as_posix()}" ruleproof hook {spec.name} '
        f'--rules "{rules.as_posix()}"'
    )
    return f"{cmd} --base {base}" if spec.base else cmd


def verify_sources(variant: str) -> None:
    """Fail fast if a rule's ``source`` line no longer holds the prose it cites."""
    import tomllib

    overlay, rules = VARIANTS[variant]
    agents_md = overlay if overlay and (overlay / "AGENTS.md").is_file() else TEMPLATE
    lines = (agents_md / "AGENTS.md").read_text(encoding="utf-8").splitlines()
    with rules.open("rb") as f:
        data = tomllib.load(f)
    for rule in data["rule"]:
        source = rule.get("source", "")
        m = re.fullmatch(r"AGENTS\.md:(\d+)", source)
        anchor = SOURCE_ANCHORS.get(rule["id"])
        if m is None or anchor is None:
            sys.exit(f"error: {rules.name}: rule {rule['id']} has no checkable source")
        line = lines[int(m.group(1)) - 1] if int(m.group(1)) <= len(lines) else ""
        if anchor not in line:
            sys.exit(f"error: {rules.name}: {rule['id']} source {source} does not hold {anchor!r}")


def agent_version(agent: str) -> str:
    try:
        res = subprocess.run(
            [resolve_exe(agent), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=child_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"unknown ({e})"
    return res.stdout.strip() or res.stderr.strip()


# --- processes --------------------------------------------------------------------------------


@dataclass
class ProcResult:
    exit_code: int | None
    timed_out: bool
    wall_s: float


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    proc.kill()


def run_proc(
    argv: Sequence[str],
    cwd: Path,
    stdin_text: str,
    stdout_path: Path,
    stderr_path: Path,
    timeout_s: float,
    env: dict[str, str],
) -> ProcResult:
    start = time.monotonic()
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=err,
            env=env,
            start_new_session=os.name != "nt",
        )
        try:
            proc.communicate(stdin_text.encode("utf-8"), timeout=timeout_s)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            proc.wait()
            return ProcResult(None, True, time.monotonic() - start)
    return ProcResult(proc.returncode, False, time.monotonic() - start)


def git(ws: Path, *args: str, env: dict[str, str] | None = None) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=ws,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env or child_env(),
        check=True,
    )
    return res.stdout


# --- workspace ---------------------------------------------------------------------------------


def rmtree(path: Path) -> None:
    """Remove a tree, including the read-only files git and uv leave on Windows."""
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except PermissionError:
        for p in path.rglob("*"):
            os.chmod(p, 0o700)
        shutil.rmtree(path)


def make_workspace(trial: Trial) -> str:
    """Create the trial's repository and return the initial commit sha."""
    ws = trial.ws
    rmtree(ws.parent)
    shutil.copytree(TEMPLATE, ws, ignore=COPY_IGNORE)
    overlay = VARIANTS[trial.arm.variant][0]
    if overlay is not None:
        shutil.copytree(overlay, ws, ignore=COPY_IGNORE, dirs_exist_ok=True)
    above = instruction_files_above(ws)
    assert not above, f"instruction files above the workspace: {above}"
    git(ws, "init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "Bench"),
        ("user.email", "bench@example.invalid"),
        ("commit.gpgsign", "false"),
        ("core.autocrlf", "false"),
    ):
        git(ws, "config", key, value)
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "Initial commit")
    base = git(ws, "rev-parse", "HEAD").strip()
    if trial.arm.hook:
        assert trial.ruleproof is not None, "hook arms need a frozen ruleproof build"
        frozen = trial.ruleproof
        install_hooks(
            ws, {ev: hook_command(frozen, h, trial.arm.rules, base) for ev, h in HOOKS.items()}
        )
    return base


def install_hooks(ws: Path, commands: dict[str, str]) -> None:
    """Register ``{hook event: command}`` as local (uncommitted, git-excluded) project settings.

    They need the initial commit's sha, so they cannot be part of that commit; as an excluded
    local file they stay out of the diff that ruleproof and the patch see.
    """
    settings = {
        "hooks": {
            event: [
                {
                    **({"matcher": HOOKS[event].matcher} if HOOKS[event].matcher else {}),
                    "hooks": [
                        {"type": "command", "command": cmd, "timeout": HOOKS[event].timeout_s}
                    ],
                }
            ]
            for event, cmd in commands.items()
        }
    }
    (ws / ".claude").mkdir(exist_ok=True)
    (ws / ".claude" / "settings.local.json").write_text(
        json.dumps(settings, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    with (ws / ".git" / "info" / "exclude").open("a", encoding="utf-8", newline="\n") as f:
        f.write(".claude/settings.local.json\n")


def save_patch(ws: Path, base: str, dest: Path) -> None:
    """Write ``base`` vs the working tree, untracked files included, without touching the index."""
    with tempfile.TemporaryDirectory() as tmp:
        env = child_env() | {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(ws, "read-tree", base, env=env)
        git(ws, "add", "-A", env=env)
        patch = git(ws, "diff", "--cached", "--binary", base, env=env)
    dest.write_text(patch, encoding="utf-8", newline="\n")


# --- transcripts -------------------------------------------------------------------------------


def json_lines(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def claude_facts(transcript: Path) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for obj in json_lines(transcript):
        if obj.get("type") == "system" and obj.get("subtype") == "init":
            facts["session_id"] = obj.get("session_id")
            facts["model"] = obj.get("model")
            facts["claude_code_version"] = obj.get("claude_code_version")
            facts["cwd"] = obj.get("cwd")
        elif obj.get("type") == "result":
            facts["result_subtype"] = obj.get("subtype")
            facts["is_error"] = obj.get("is_error")
            facts["cost_usd"] = obj.get("total_cost_usd")
            facts["num_turns"] = obj.get("num_turns")
            facts["usage"] = obj.get("usage")
            model_usage = obj.get("modelUsage")
            if isinstance(model_usage, dict):
                facts["models_used"] = sorted(model_usage)
    return facts


def codex_facts(transcript: Path) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    in_tok = cached = out_tok = turns = 0
    errors: list[str] = []
    for obj in json_lines(transcript):
        kind = obj.get("type")
        if kind == "thread.started":
            facts["session_id"] = obj.get("thread_id")
        elif kind == "turn.completed":
            turns += 1
            usage = obj.get("usage")
            if isinstance(usage, dict):
                in_tok += int(usage.get("input_tokens") or 0)
                cached += int(usage.get("cached_input_tokens") or 0)
                out_tok += int(usage.get("output_tokens") or 0)
        elif kind in ("turn.failed", "error"):
            errors.append(json.dumps(obj)[:300])
    facts["num_turns"] = turns
    facts["usage"] = {
        "input_tokens": in_tok,
        "cached_input_tokens": cached,
        "output_tokens": out_tok,
    }
    facts["is_error"] = bool(errors) and turns == 0
    if errors:
        facts["errors"] = errors[-3:]
    return facts


_RULE_IN_REASON = re.compile(r"^\s*(?:\d+\.|-)?\s*([a-z][\w.-]*[a-z0-9]):", re.MULTILINE)


def _rule_ids(reason: str) -> list[str]:
    return [r for r in _RULE_IN_REASON.findall(reason) if r != "ruleproof"]


def hook_facts(transcript: Path) -> dict[str, Any]:
    """Count hook runs, Stop blocks and PreToolUse denials from ``--include-hook-events``."""
    facts: dict[str, Any] = {
        "hook_runs": 0,  # Stop
        "hook_blocks": 0,
        "hook_blocked_rules": [],
        "pretool_runs": 0,
        "pretool_denies": 0,
        "pretool_denied_rules": [],
        "hook_failures": 0,  # either hook exited non-zero or reported an error
    }
    for obj in json_lines(transcript):
        event = obj.get("hook_event")
        if obj.get("subtype") != "hook_response" or event not in HOOKS:
            continue
        output = obj.get("output") or obj.get("stdout") or ""
        try:
            decision = json.loads(output) if output.strip() else {}
        except ValueError:
            decision = {}
        if not isinstance(decision, dict):
            decision = {}
        specific = decision.get("hookSpecificOutput")
        specific = specific if isinstance(specific, dict) else {}
        failed = obj.get("exit_code") not in (0, None) or obj.get("outcome") not in (
            "success",
            None,
        )
        if event == "Stop":
            facts["hook_runs"] += 1
            if decision.get("decision") == "block":
                facts["hook_blocks"] += 1
                reason = str(decision.get("reason", ""))
                facts["hook_blocked_rules"] += _rule_ids(reason)
                continue
        else:
            facts["pretool_runs"] += 1
            if specific.get("permissionDecision") == "deny" or decision.get("decision") == "block":
                facts["pretool_denies"] += 1
                reason = str(specific.get("permissionDecisionReason") or decision.get("reason"))
                facts["pretool_denied_rules"] += _rule_ids(reason)
                continue
        if failed:
            facts["hook_failures"] += 1
    return facts


def copy_claude_session(session_id: str, ws: Path, out: Path) -> str | None:
    projects = Path.home() / ".claude" / "projects"
    encoded = re.sub(r"[^A-Za-z0-9]", "-", str(ws))
    candidates = [projects / encoded / f"{session_id}.jsonl"]
    candidates += list(projects.glob(f"*/{session_id}.jsonl"))
    for src in candidates:
        if src.is_file():
            shutil.copy2(src, out / "session.jsonl")
            sub = src.with_suffix("") / "subagents"
            if sub.is_dir():
                shutil.copytree(sub, out / "subagents", dirs_exist_ok=True)
            return str(src)
    return None


def copy_codex_rollout(
    codex_home: Path, thread_id: str, started: float, out: Path
) -> tuple[str | None, str | None]:
    """Copy the rollout file for ``thread_id``; return (source path, model)."""
    sessions = codex_home / "sessions"
    day_dirs = {
        sessions / datetime.fromtimestamp(t).strftime("%Y/%m/%d")
        for t in (started - 86400, started, time.time())
    }
    for day in sorted(day_dirs):
        for src in day.glob(f"rollout-*{thread_id}.jsonl"):
            shutil.copy2(src, out / "rollout.jsonl")
            model = None
            for obj in json_lines(src):
                payload = obj.get("payload")
                if obj.get("type") == "turn_context" and isinstance(payload, dict):
                    model = payload.get("model") or model
            return str(src), model
    return None, None


# --- grading -----------------------------------------------------------------------------------


def grade(trial: Trial, out: Path) -> dict[str, Any]:
    """Run the agent's suite and then the hidden test in a copy of the final workspace."""
    if trial.task.hidden_test is None:
        return {}
    gdir = trial.ws.parent / "grade"
    rmtree(gdir)
    shutil.copytree(trial.ws, gdir, ignore=shutil.ignore_patterns(".git", *CACHE_DIRS))
    hidden_name = f"test_zz_hidden_{trial.task.name.replace('-', '_')}.py"
    shutil.copy2(trial.task.hidden_test, gdir / "tests" / hidden_name)
    uv = resolve_exe("uv")
    results: dict[str, Any] = {}
    with (out / "grade.log").open("w", encoding="utf-8") as logf:
        for label, extra in (
            ("suite", ["--ignore", f"tests/{hidden_name}"]),
            ("hidden", [f"tests/{hidden_name}"]),
        ):
            argv = [uv, "run", "pytest", "-q", "-p", "no:cacheprovider", *extra]
            try:
                res = subprocess.run(
                    argv,
                    cwd=gdir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=child_env(),
                    timeout=GRADE_TIMEOUT_S,
                )
                code: int | None = res.returncode
                text = res.stdout + res.stderr
            except subprocess.TimeoutExpired:
                code, text = None, "timed out\n"
            logf.write(f"===== {label}: {' '.join(argv[1:])} -> {code}\n{text}\n")
            results[f"{label}_pass"] = code == 0
            results[f"{label}_exit"] = code
    rmtree(gdir)
    return results


# --- one trial ---------------------------------------------------------------------------------


def run_trial(trial: Trial, versions: dict[str, str]) -> dict[str, Any]:
    out = trial.out
    rmtree(out)
    out.mkdir(parents=True)
    meta: dict[str, Any] = {
        "run_id": trial.run_id,
        "arm": trial.arm.name,
        "agent": trial.arm.agent,
        "task": trial.task.name,
        "rep": trial.rep,
        "agent_version": versions.get(trial.arm.agent),
        "model_alias": trial.arm.model,
        "variant": trial.arm.variant,
        "rules": trial.arm.rules.name,
        "hook": trial.arm.hook,
        "workspace": str(trial.ws),
        "started_at": now_iso(),
        "complete": False,
    }
    base = make_workspace(trial)
    meta["base_sha"] = base
    argv = agent_argv(trial.arm, trial.ws)
    meta["argv"] = [Path(argv[0]).name, *argv[1:]]
    (out / "prompt.md").write_text(trial.task.prompt, encoding="utf-8", newline="\n")

    transcript = out / "transcript.jsonl"
    started = time.time()
    codex_home = trial.codex_home if trial.arm.agent == "codex" else None
    meta["codex_home"] = "separate" if codex_home else "user"
    if trial.arm.hook and trial.ruleproof is not None:
        meta["ruleproof_commit"] = trial.ruleproof.commit
    env = child_env(codex_home)
    proc = run_proc(
        argv, trial.ws, trial.task.prompt, transcript, out / "stderr.txt", TRIAL_TIMEOUT_S, env
    )
    meta.update(exit_code=proc.exit_code, timed_out=proc.timed_out, wall_s=round(proc.wall_s, 1))

    if trial.arm.agent == "claude":
        facts = claude_facts(transcript)
        if trial.arm.hook:
            facts.update(hook_facts(transcript))
        sid = facts.get("session_id")
        meta["session_file"] = copy_claude_session(sid, trial.ws, out) if sid else None
    else:
        facts = codex_facts(transcript)
        sid = facts.get("session_id")
        if sid:
            home = trial.codex_home or Path.home() / ".codex"
            meta["session_file"], facts["model"] = copy_codex_rollout(home, sid, started, out)
    meta.update(facts)

    save_patch(trial.ws, base, out / "diff.patch")
    head = git(trial.ws, "rev-parse", "HEAD").strip()
    meta["head_sha"] = head
    meta["committed"] = head != base
    meta.update(grade(trial, out))

    rmtree(out / "workspace")
    shutil.copytree(trial.ws, out / "workspace", ignore=COPY_IGNORE)

    meta["error"] = trial_error(meta, transcript)
    meta["finished_at"] = now_iso()
    meta["complete"] = True
    write_json(out / "meta.json", meta)
    return meta


def trial_error(meta: dict[str, Any], transcript: Path) -> str | None:
    """A harness or provider failure (not the agent doing the task badly), else None."""
    if meta.get("timed_out"):
        return None  # a legitimate outcome, recorded as such
    if meta.get("session_id") is None:
        return f"no session in transcript (exit {meta.get('exit_code')})"
    if meta["agent"] == "claude":
        subtype = meta.get("result_subtype")
        if subtype is None:
            return "no result event"
        if meta.get("is_error") and subtype not in ("error_max_budget_usd", "error_max_turns"):
            return f"result {subtype}"
        wanted = meta.get("model_alias")
        if wanted and wanted.startswith("claude-") and meta.get("model") != wanted:
            # An explicit model id must be honoured, not silently replaced.
            return f"asked for model {wanted}, transcript reports {meta.get('model')}"
    elif meta.get("is_error"):
        return "codex: " + "; ".join(meta.get("errors", []))[:300]
    if transcript.stat().st_size == 0:
        return "empty transcript"
    return None


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def is_done(trial: Trial) -> bool:
    meta = trial.out / "meta.json"
    if not meta.is_file():
        return False
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except ValueError:
        return False
    return bool(data.get("complete")) and data.get("error") is None


# --- CLI ---------------------------------------------------------------------------------------


def load_tasks(spec: str) -> list[Task]:
    available = sorted(p.name for p in TASKS.iterdir() if (p / "prompt.md").is_file())
    names = available if spec == "all" else [s.strip() for s in spec.split(",") if s.strip()]
    tasks = []
    for name in names:
        if name not in available:
            sys.exit(f"error: unknown task {name!r} (available: {', '.join(available)})")
        d = TASKS / name
        hidden = d / "hidden_test.py"
        tasks.append(
            Task(
                name,
                (d / "prompt.md").read_text(encoding="utf-8"),
                hidden if hidden.is_file() else None,
            )
        )
    return tasks


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    p.add_argument("--arms", default="claude-sonnet,codex", help="comma-separated arm names")
    p.add_argument("--tasks", default="all", help="comma-separated task names, or 'all'")
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--run-id", required=True)
    p.add_argument(
        "--pilot",
        nargs="?",
        const=str(PILOT_PROMPT),
        metavar="PROMPT_FILE",
        help="run a pilot prompt instead of the tasks (default: the isolation pilot)",
    )
    p.add_argument(
        "--ruleproof-ref",
        help="ruleproof commit the hook arms run, from a frozen worktree (required for them)",
    )
    p.add_argument(
        "--codex-home",
        help="CODEX_HOME for the codex arm (logged in, no AGENTS.md); default: the user's",
    )
    p.add_argument(
        "--max-errors",
        type=int,
        default=3,
        help="stop starting new trials once more than this many errored",
    )
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.run_id):
        sys.exit("error: --run-id may only contain letters, digits, '.', '_' and '-'")
    arms = []
    for name in args.arms.split(","):
        if name.strip() not in ARMS:
            sys.exit(f"error: unknown arm {name!r} (available: {', '.join(ARMS)})")
        arms.append(ARMS[name.strip()])
    if args.pilot:
        prompt = Path(args.pilot)
        name = "pilot" if prompt == PILOT_PROMPT else f"pilot-{prompt.stem}"
        tasks = [Task(name, prompt.read_text(encoding="utf-8"), None)]
    else:
        tasks = load_tasks(args.tasks)

    codex_home = Path(args.codex_home).resolve() if args.codex_home else None
    if codex_home is not None:
        if not codex_home.is_dir():
            sys.exit(f"error: {codex_home} does not exist; log in with CODEX_HOME set to it")
        stray = [n for n in ("AGENTS.md", "AGENTS.override.md") if (codex_home / n).exists()]
        if stray:
            sys.exit(f"error: {codex_home} contains {', '.join(stray)}")
    frozen = None
    if any(a.hook for a in arms):
        if not args.ruleproof_ref:
            sys.exit("error: hook arms need --ruleproof-ref (a commit to freeze ruleproof at)")
        frozen = freeze_ruleproof(args.ruleproof_ref)
        log(f"hook arms run ruleproof {frozen.commit} from {frozen.root}")
    for variant in sorted({a.variant for a in arms}):
        verify_sources(variant)
    above = instruction_files_above(WORK_ROOT)
    if above:
        sys.exit(f"error: instruction files above {WORK_ROOT}: {above}")

    versions = {a.agent: agent_version(a.agent) for a in arms}
    trials = [
        Trial(args.run_id, arm, task, rep, codex_home, frozen if arm.hook else None)
        for rep in range(1, args.reps + 1)
        for task in tasks
        for arm in arms
    ]
    todo = [t for t in trials if not is_done(t)]
    log(f"{len(trials)} trials, {len(trials) - len(todo)} already done; versions: {versions}")

    errors = 0
    stop = threading.Event()

    def guarded(trial: Trial) -> dict[str, Any] | None:
        if stop.is_set():
            return None
        log(f"start {trial.key}")
        return run_trial(trial, versions)

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(guarded, t): t for t in todo}
        for fut in as_completed(futures):
            trial = futures[fut]
            try:
                meta = fut.result()
            except Exception as e:  # a harness bug must not take the other trials down
                errors += 1
                log(f"CRASH {trial.key}: {type(e).__name__}: {e}")
            else:
                if meta is None:
                    continue
                if meta.get("error"):
                    errors += 1
                log(
                    f"done  {trial.key}: exit={meta.get('exit_code')} "
                    f"wall={meta.get('wall_s')}s cost={meta.get('cost_usd')} "
                    f"hidden={meta.get('hidden_pass')} error={meta.get('error')}"
                )
            if errors > args.max_errors and not stop.is_set():
                stop.set()
                log(f"ABORT: {errors} trials errored; not starting more")
    return 1 if stop.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
