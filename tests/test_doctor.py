from __future__ import annotations

import json
from pathlib import Path

import pytest

from ruleproof.doctor import DOCTOR_CHECKS, run_doctor
from ruleproof.doctor.skills import parse_frontmatter
from ruleproof.errors import ConfigError
from ruleproof.models import RuleResult


def make(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def ids(results: list[RuleResult]) -> list[str]:
    return [r.rule.id for r in results]


def of(results: list[RuleResult], check: str) -> list[RuleResult]:
    return [r for r in results if r.rule.id == f"doctor/{check}"]


AGENTS = """\
# Project guide

## Setup

Run `pnpm install` to install dependencies.

## Testing

- Run `pnpm test` before you finish.
- Never commit generated files in `dist/`.

## Style

- Format with `pnpm run format`.
- Use 2 spaces for indentation.

See [the contributing guide](docs/contributing.md) and `src/index.ts`.
"""

PACKAGE = json.dumps(
    {
        "name": "demo",
        "packageManager": "pnpm@9.1.0",
        "scripts": {"test": "vitest run", "format": "prettier -w ."},
        "devDependencies": {"vitest": "^2.0.0", "prettier": "^3.0.0"},
    }
)

SKILL = """\
---
name: release-notes
description: >-
  Draft release notes from merged pull requests. Use when the user asks for a
  changelog or release summary.
---

# Release notes

Run `scripts/collect.py` and summarise the output.
"""


def clean_repo(root: Path) -> Path:
    return make(
        root,
        {
            "AGENTS.md": AGENTS,
            "CLAUDE.md": "@AGENTS.md\n\n## Claude only\n\n- Prefer small commits.\n",
            "package.json": PACKAGE,
            "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
            "src/index.ts": "export {};\n",
            "docs/contributing.md": "# Contributing\n",
            ".claude/skills/release-notes/SKILL.md": SKILL,
            ".claude/skills/release-notes/scripts/collect.py": "print('hi')\n",
            ".agents/skills/release-notes/SKILL.md": SKILL,
            ".agents/skills/release-notes/scripts/collect.py": "print('hi')\n",
            ".mcp.json": json.dumps(
                {
                    "mcpServers": {
                        "github": {
                            "command": "npx",
                            "args": ["-y", "@modelcontextprotocol/server-github"],
                            "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
                        }
                    }
                }
            ),
            ".cursor/mcp.json": json.dumps(
                {
                    "mcpServers": {
                        "github": {
                            "command": "npx",
                            "args": ["@modelcontextprotocol/server-github"],
                            "env": {"GITHUB_TOKEN": "${env:GITHUB_TOKEN}"},
                        }
                    }
                }
            ),
            ".vscode/mcp.json": (
                '{\n  // shared servers\n  "servers": {\n    "github": {\n'
                '      "command": "npx",\n'
                '      "args": ["-y", "@modelcontextprotocol/server-github"],\n'
                "    },\n  },\n}\n"
            ),
        },
    )


def test_clean_repo_has_no_findings(tmp_path: Path) -> None:
    assert run_doctor(clean_repo(tmp_path)) == []


def test_checks_are_documented() -> None:
    assert set(DOCTOR_CHECKS) == {
        "drift",
        "conflict",
        "repo-mismatch",
        "dead-reference",
        "size",
        "skill",
        "mcp-drift",
    }


def test_not_a_directory(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        run_doctor(tmp_path / "missing")


def test_result_shape_and_order(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "CLAUDE.md": "Read @docs/missing.md first.\n",
            "AGENTS.md": "x" * 12000,
        },
    )
    results = run_doctor(tmp_path)
    assert [r.rule.severity for r in results] == sorted(
        (r.rule.severity for r in results), key=["error", "warning", "info"].index
    )
    first = results[0]
    assert first.status == "fail"
    assert first.rule.check == "doctor"
    assert first.rule.origin == "doctor"
    assert first.rule.source is None
    assert first.rule.description == DOCTOR_CHECKS["dead-reference"]
    assert first.evidence[0].path == "CLAUDE.md"
    assert first.evidence[0].line == 1


# --- drift ----------------------------------------------------------------------------------

DRIFT_CLAUDE = """\
# CLAUDE.md

## Testing

Run `uv run pytest -q` before finishing.

## Style

Use type hints everywhere and keep functions small.
"""

DRIFT_AGENTS = """\
# AGENTS.md

## Setup

Install with `uv sync`.

## Release

Tag the commit and push the tag.
"""


def test_drift_reports_sections_and_commands(tmp_path: Path) -> None:
    make(tmp_path, {"CLAUDE.md": DRIFT_CLAUDE, "AGENTS.md": DRIFT_AGENTS, "uv.lock": ""})
    found = of(run_doctor(tmp_path), "drift")
    assert len(found) == 1
    summary = found[0].summary
    assert "CLAUDE.md has 'Testing' (`uv run pytest -q`)" in summary
    assert "AGENTS.md has no such section" in summary
    assert "AGENTS.md has 'Setup' (`uv sync`)" in summary
    assert found[0].rule.severity == "warning"
    assert {(e.path, e.line) for e in found[0].evidence} >= {("CLAUDE.md", 3), ("AGENTS.md", 3)}


@pytest.mark.parametrize(
    "claude",
    [
        "@AGENTS.md\n\n## Testing\n\nRun `uv run pytest -q`.\n",
        "Read [AGENTS.md](AGENTS.md) first.\n\n## Testing\n\nRun `uv run pytest -q`.\n",
        DRIFT_AGENTS,
    ],
)
def test_drift_not_reported_when_linked_or_identical(tmp_path: Path, claude: str) -> None:
    make(tmp_path, {"CLAUDE.md": claude, "AGENTS.md": DRIFT_AGENTS, "uv.lock": ""})
    assert [r for r in of(run_doctor(tmp_path), "drift") if r.rule.severity != "info"] == []


def test_drift_follows_claude_imports(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "CLAUDE.md": "@docs/agent-guide.md\n",
            "docs/agent-guide.md": DRIFT_AGENTS,
            "AGENTS.md": DRIFT_AGENTS,
            "uv.lock": "",
        },
    )
    assert of(run_doctor(tmp_path), "drift") == []


def test_pointer_without_import_is_info(tmp_path: Path) -> None:
    make(tmp_path, {"CLAUDE.md": "See AGENTS.md for instructions.\n", "AGENTS.md": DRIFT_AGENTS})
    found = of(run_doctor(tmp_path), "drift")
    assert len(found) == 1
    assert found[0].rule.severity == "info"
    assert "`@AGENTS.md`" in found[0].summary


def test_gemini_configured_to_read_agents_md(tmp_path: Path) -> None:
    files = {"AGENTS.md": DRIFT_AGENTS, "GEMINI.md": DRIFT_CLAUDE, "uv.lock": ""}
    make(tmp_path, files)
    assert len(of(run_doctor(tmp_path), "drift")) == 1
    make(tmp_path, {".gemini/settings.json": '{"context": {"fileName": ["AGENTS.md"]}}'})
    assert of(run_doctor(tmp_path), "drift") == []


# --- conflict -------------------------------------------------------------------------------


def test_package_manager_conflict_across_files(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "CLAUDE.md": "@AGENTS.md\n\nInstall with `npm install`.\n",
            "AGENTS.md": "Install with `pnpm install`.\n",
        },
    )
    found = of(run_doctor(tmp_path), "conflict")
    assert len(found) == 1
    assert "JavaScript package manager" in found[0].summary
    assert {e.path for e in found[0].evidence} == {"CLAUDE.md", "AGENTS.md"}


@pytest.mark.parametrize(
    "text",
    [
        "Use `pnpm`, not `npm`.\n",
        "We migrated from npm to pnpm; run `pnpm install`.\n",
        "Run `pnpm install` instead of `npm install`.\n",
        "Install with `npm install` or `pnpm install`.\n",
        "Use pnpm. Don't use `npm install -g` for project deps.\n- `npm install -g pnpm`\n",
        "Use uv: `uv sync`, then `uv run pytest`. Install uv with `pip install uv`.\n",
        "Lint with ruff (`ruff check`) and format with `black .`.\n",
    ],
)
def test_no_tool_conflict(tmp_path: Path, text: str) -> None:
    make(tmp_path, {"AGENTS.md": text})
    assert of(run_doctor(tmp_path), "conflict") == []


def test_tool_conflicts_by_job(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "AGENTS.md": (
                "- Run tests with `uv run pytest`.\n"
                "- Run tests with `python -m unittest discover`.\n"
                "- Format with `black .`.\n"
                "- Format with `uv run ruff format`.\n"
            )
        },
    )
    summaries = [r.summary for r in of(run_doctor(tmp_path), "conflict")]
    assert any("Python test runner" in s for s in summaries)
    assert any("Python formatter" in s and "ruff format" in s for s in summaries)


def test_subproject_scoped_tools_do_not_conflict(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "AGENTS.md": "Run `pnpm install` at the root.\n\n## docs\n\nRun `npm install` there.\n",
            "package.json": "{}",
            "docs/package.json": "{}",
        },
    )
    assert of(run_doctor(tmp_path), "conflict") == []


def test_indentation_conflict(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "AGENTS.md": "- Indent with 2 spaces.\n- Python: indent with 4 spaces.\n",
            "GEMINI.md": "@AGENTS.md\n\n- Indent with tabs.\n",
        },
    )
    found = of(run_doctor(tmp_path), "conflict")
    assert len(found) == 1
    assert "2 spaces" in found[0].summary and "tabs" in found[0].summary


def test_always_never_conflict(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "AGENTS.md": "- Always run the linter before committing.\n",
            "CLAUDE.md": "@AGENTS.md\n\n- Never run the linter before committing.\n",
        },
    )
    found = of(run_doctor(tmp_path), "conflict")
    assert len(found) == 1
    assert {(e.path, e.line) for e in found[0].evidence} == {("AGENTS.md", 1), ("CLAUDE.md", 3)}


def test_dont_list_does_not_conflict_with_never(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "AGENTS.md": (
                "## Don'ts\n\n- Commit secrets or API keys.\n\n"
                "## Rules\n\n- Never commit secrets or API keys.\n"
                "- Don't add comments. Add comments where the why is non-obvious.\n"
            )
        },
    )
    assert of(run_doctor(tmp_path), "conflict") == []


# --- repo-mismatch --------------------------------------------------------------------------


def test_package_manager_mismatch(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "AGENTS.md": "Install with `npm install`.\n\nThen `pip install -e .`.\n",
            "package.json": "{}",
            "pnpm-lock.yaml": "",
            "pyproject.toml": "[project]\nname = 'x'\n",
            "uv.lock": "",
        },
    )
    found = of(run_doctor(tmp_path), "repo-mismatch")
    summaries = sorted(r.summary for r in found)
    assert summaries == [
        "AGENTS.md:1 uses npm but the repo uses pnpm (pnpm-lock.yaml)",
        "AGENTS.md:3 uses pip but the repo uses uv (uv.lock)",
    ]


def test_test_runner_mismatch(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "AGENTS.md": "Run `npx jest` and `python -m unittest`.\n",
            "package.json": json.dumps({"devDependencies": {"vitest": "2"}}),
            "pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n",
        },
    )
    summaries = sorted(r.summary for r in of(run_doctor(tmp_path), "repo-mismatch"))
    assert summaries == [
        "AGENTS.md:1 uses jest but the repo uses vitest (package.json)",
        "AGENTS.md:1 uses unittest but the repo uses pytest (pyproject.toml configures pytest)",
    ]


def test_version_mismatch(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "AGENTS.md": (
                "Requires Python 3.10+ and Node 18.\n"
                "Supports Python 3.11 through 3.13.\n"
                "We no longer support Python 3.8.\n"
            ),
            "pyproject.toml": '[project]\nrequires-python = ">=3.11"\n',
            ".nvmrc": "v20.11.0\n",
        },
    )
    summaries = sorted(r.summary for r in of(run_doctor(tmp_path), "repo-mismatch"))
    assert summaries == [
        "AGENTS.md:1 says Node 18 but .nvmrc pins 20",
        'AGENTS.md:1 says Python 3.10+ but pyproject.toml requires-python ">=3.11" sets the '
        "minimum to 3.11",
    ]


def test_matching_versions_are_fine(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "AGENTS.md": "Python 3.11+, developed on Python 3.12. Node.js 20 or newer.\n",
            "pyproject.toml": '[project]\nrequires-python = ">=3.11"\n',
            ".python-version": "3.12.4\n",
            "package.json": json.dumps({"engines": {"node": ">=20"}}),
        },
    )
    assert of(run_doctor(tmp_path), "repo-mismatch") == []


# --- dead-reference -------------------------------------------------------------------------


def test_dead_references(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "CLAUDE.md": "@docs/missing.md\n@docs/present.md\n",
            "docs/present.md": "Nothing here.\n",
            "AGENTS.md": (
                "See [design](docs/design.md) and [site](https://example.com).\n"
                "Entry point is `src/app.py`; config lives in `config/settings.toml`.\n"
                "Run `npm run lint` and `npm test` and `make check` and `just deploy`.\n"
                "Ignore `*.pyc`, `<name>.md`, `path/to/file.py`, `dist/bundle.js`.\n"
                "Email someone@example.com or ping @maintainer.\n"
            ),
            "src/main.py": "",
            "package.json": json.dumps({"scripts": {"test": "vitest"}}),
            "Makefile": ".PHONY: test\ntest:\n\tpytest\nbuild: dist\n",
            "justfile": "default:\n    just --list\n\nrelease version:\n    echo {{version}}\n",
        },
    )
    found = of(run_doctor(tmp_path), "dead-reference")
    by_summary = {r.summary: r.rule.severity for r in found}
    assert by_summary == {
        "CLAUDE.md:1 imports @docs/missing.md, which does not exist; "
        "Claude Code skips it silently": "error",
        "AGENTS.md:1 links to docs/design.md, which does not exist": "warning",
        "AGENTS.md:2 mentions `src/app.py`, which does not exist in the repo": "warning",
        "AGENTS.md:2 mentions `config/settings.toml`, which does not exist in the repo": "warning",
        'AGENTS.md:3 runs `npm run lint` but package.json has no "lint" script': "warning",
        'AGENTS.md:3 runs `make check` but Makefile has no "check" target': "warning",
        'AGENTS.md:3 runs `just deploy` but justfile has no "deploy" recipe': "warning",
    }


def test_references_resolve_relative_and_by_suffix(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "web/AGENTS.md": (
                "Edit `components/Button.tsx` and `../README.md`; see [api](./api.md).\n"
                "Run `pnpm dev` and `make -C .. test`.\n"
            ),
            "web/api.md": "",
            "web/src/components/Button.tsx": "",
            "web/package.json": json.dumps({"scripts": {"dev": "vite"}}),
            "README.md": "",
            "Makefile": "test lint:\n\tpytest\n",
        },
    )
    assert of(run_doctor(tmp_path), "dead-reference") == []


def test_negative_and_creation_lines_are_not_dead_references(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "AGENTS.md": (
                "Never commit `secrets.json`.\n"
                "Create `notes/todo.md` when you start.\n"
                "Write tests in `tests/test_new_feature.py`.\n"
            )
        },
    )
    assert of(run_doctor(tmp_path), "dead-reference") == []


def test_unknown_make_targets_are_not_guessed(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "AGENTS.md": "Run `make anything`.\n",
            "Makefile": "include common.mk\n",
        },
    )
    assert of(run_doctor(tmp_path), "dead-reference") == []


# --- size -----------------------------------------------------------------------------------


def test_size_counts_imports(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "CLAUDE.md": "@docs/big.md\n" + "a" * 4000,
            "docs/big.md": "b" * 8000,
            "AGENTS.md": "c" * 4000,
        },
    )
    found = of(run_doctor(tmp_path, max_tokens=1000), "size")
    assert len(found) == 1
    assert found[0].rule.severity == "warning"
    assert found[0].summary.startswith("Claude Code loads about 3,004 tokens")
    assert "docs/big.md 2,000" in found[0].summary
    assert "estimated" in found[0].summary


def test_size_info_between_budget_and_double(tmp_path: Path) -> None:
    make(tmp_path, {"AGENTS.md": "c" * 6000})
    found = of(run_doctor(tmp_path, max_tokens=1000), "size")
    assert [r.rule.severity for r in found] == ["info"]
    assert found[0].summary.startswith("Codex loads about 1,500 tokens")


def test_import_cycles_are_safe(tmp_path: Path) -> None:
    make(tmp_path, {"CLAUDE.md": "@a.md\n", "a.md": "@b.md\n", "b.md": "@a.md\n@CLAUDE.md\n"})
    assert run_doctor(tmp_path) == []


# --- skills ---------------------------------------------------------------------------------


def test_skill_problems(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            ".claude/skills/no-front/SKILL.md": "# Just a heading\n",
            ".claude/skills/mismatch/SKILL.md": (
                "---\nname: Other_Name\ndescription: Does things.\n---\n"
                "Run `scripts/missing.sh` and read [ref](references/guide.md).\n"
            ),
            ".claude/skills/long/SKILL.md": f"---\nname: long\ndescription: {'d' * 1100}\n---\n",
            ".claude/skills/bare/SKILL.md": "---\nname: bare\n---\nBody\n",
            ".codex/skills/wrong-case/skill.md": "---\nname: wrong-case\ndescription: x\n---\n",
            ".claude/skills/dup/SKILL.md": "---\nname: dup\ndescription: One.\n---\n",
            ".agents/skills/dup/SKILL.md": "---\nname: dup\ndescription: Two.\n---\n",
        },
    )
    found = of(run_doctor(tmp_path), "skill")
    summaries = {r.summary: r.rule.severity for r in found}
    expected = {
        ".claude/skills/no-front/SKILL.md has no YAML frontmatter; agents need `name` and "
        "`description` to discover the skill": "error",
        ".claude/skills/mismatch/SKILL.md name 'Other_Name' must be lowercase letters, digits "
        "and hyphens, at most 64 characters": "warning",
        ".claude/skills/mismatch/SKILL.md name 'Other_Name' does not match its directory "
        "'mismatch'": "warning",
        ".claude/skills/mismatch/SKILL.md:5 refers to scripts/missing.sh, which is missing "
        "from the skill directory": "warning",
        ".claude/skills/mismatch/SKILL.md:5 refers to references/guide.md, which is missing "
        "from the skill directory": "warning",
        ".claude/skills/long/SKILL.md description is 1,100 characters (limit 1,024)": "warning",
        ".claude/skills/bare/SKILL.md has no `description`; agents use it to decide when to "
        "load the skill": "error",
        ".codex/skills/wrong-case/skill.md must be named SKILL.md; agents do not load "
        "skill.md": "error",
        "Skill 'dup' has copies with different content: .claude/skills/dup/SKILL.md, "
        ".agents/skills/dup/SKILL.md": "warning",
    }
    assert summaries == expected


def test_frontmatter_parser() -> None:
    fm = parse_frontmatter(
        "---\nname: 'quoted'\ndescription: |\n  line one\n  line two\nother: plain # note\n"
        "metadata:\n  owner: me\n---\nbody\n"
    )
    assert fm is not None
    assert fm.fields["name"] == "quoted"
    assert fm.fields["description"] == "line one\nline two"
    assert fm.fields["other"] == "plain"
    assert fm.lines["description"] == 3
    assert parse_frontmatter("---\nname: x\n") is None
    assert parse_frontmatter("no frontmatter") is None


# --- mcp ------------------------------------------------------------------------------------

FAKE_TOKEN = "gh" + "p_" + "Zq3kV8" * 6


def test_mcp_drift_and_secrets(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            ".mcp.json": json.dumps(
                {
                    "mcpServers": {
                        "docs": {"command": "npx", "args": ["-y", "docs-server@1"]},
                        "gh": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": FAKE_TOKEN}},
                    }
                },
                indent=2,
            ),
            ".codex/config.toml": (
                '[mcp_servers.docs]\ncommand = "npx"\nargs = ["docs-server@2"]\n\n'
                '[mcp_servers.api]\nurl = "https://api.example.com/mcp"\n'
                '[mcp_servers.api.http_headers]\nAuthorization = "Bearer ${API_TOKEN}"\n'
            ),
        },
    )
    found = of(run_doctor(tmp_path), "mcp-drift")
    assert [r.rule.severity for r in found] == ["error", "warning"]
    secret, drift = found
    assert FAKE_TOKEN not in secret.summary
    assert all(FAKE_TOKEN not in (e.excerpt or "") for e in secret.evidence)
    assert "ghp_…(40 chars)" in secret.summary
    assert "env.GITHUB_TOKEN" in secret.summary
    assert secret.evidence[0].path == ".mcp.json"
    assert secret.evidence[0].line == 13
    assert drift.summary == (
        "MCP server 'docs' differs between .mcp.json and .codex/config.toml: "
        "args `docs-server@1` vs `docs-server@2`"
    )
    assert {e.path for e in drift.evidence} == {".mcp.json", ".codex/config.toml"}


def test_mcp_invalid_files(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            ".mcp.json": '{\n  "mcpServers": {\n    "a": {"command": "x",}\n  }\n}\n',
            ".codex/config.toml": "[mcp_servers.a\ncommand = 1\n",
        },
    )
    found = of(run_doctor(tmp_path), "mcp-drift")
    assert sorted((r.evidence[0].path, r.evidence[0].line) for r in found) == [
        (".codex/config.toml", 1),
        (".mcp.json", 3),
    ]
    assert all("not valid" in r.summary for r in found)


def test_mcp_windows_wrapper_is_not_drift(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            ".mcp.json": json.dumps(
                {"mcpServers": {"fs": {"command": "cmd", "args": ["/c", "npx", "-y", "fs-mcp"]}}}
            ),
            ".cursor/mcp.json": json.dumps(
                {"mcpServers": {"fs": {"command": "npx", "args": ["fs-mcp"]}}}
            ),
        },
    )
    assert run_doctor(tmp_path) == []


# --- exclude --------------------------------------------------------------------------------


def test_exclude_skips_files(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "AGENTS.md": "Fine.\n",
            "vendor-docs/AGENTS.md": "See [x](missing.md).\n",
            "legacy/CLAUDE.md": "@gone.md\n",
            ".claude/skills/old/SKILL.md": "no frontmatter\n",
            ".mcp.json": "{ not json",
        },
    )
    assert len(run_doctor(tmp_path)) == 4
    excluded = run_doctor(
        tmp_path, exclude=["vendor-docs/", "legacy/**", ".claude/skills/old/", ".mcp.json"]
    )
    assert excluded == []


# --- robustness -----------------------------------------------------------------------------


def test_negated_tool_lists(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "AGENTS.md": (
                "Use `uv` for everything. Do not invoke `pip`, `poetry`, or `pdm` directly.\n"
            ),
            "uv.lock": "",
        },
    )
    assert run_doctor(tmp_path) == []


def test_crlf_bom_and_symlink_standin(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_bytes(
        "﻿## Testing\r\n\r\nRun `make test`.\r\n".encode() + b"See [x](missing.md).\r\n"
    )
    (tmp_path / "Makefile").write_bytes(b"test:\r\n\tpytest\r\n")
    (tmp_path / "CLAUDE.md").write_text("AGENTS.md", encoding="utf-8")  # git symlink checkout
    found = run_doctor(tmp_path)
    assert [(r.rule.id, r.evidence[0].line) for r in found] == [("doctor/dead-reference", 4)]


def test_file_names_are_case_sensitive(tmp_path: Path) -> None:
    make(tmp_path, {"AGENTS.md": DRIFT_AGENTS, "docs/gemini.md": "", "gemini.md": DRIFT_CLAUDE})
    assert of(run_doctor(tmp_path), "drift") == []
