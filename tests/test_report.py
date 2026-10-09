from __future__ import annotations

import json

import pytest

from ruleproof.errors import ConfigError
from ruleproof.models import Evidence, Report, Rule, RuleResult, SessionInfo
from ruleproof.report import FORMATS, PROJECT_URL, markdown, render, sarif, split_source, text
from ruleproof.report import json as json_report


def _rule(rid: str, **kw: object) -> Rule:
    base: dict[str, object] = {
        "check": "forbid-change",
        "description": f"description of {rid}",
        "source": "AGENTS.md:12",
        "origin": "ruleproof.toml:3",
    }
    base.update(kw)
    return Rule(id=rid, **base)  # type: ignore[arg-type]


def make_report() -> Report:
    return Report(
        kind="check",
        tool_version="9.9.9",
        repo="/repo",
        base="HEAD",
        session=SessionInfo("claude-code", "abc123", "/t/abc123.jsonl"),
        results=[
            RuleResult(_rule("passes-1"), "pass", "no backup files"),
            RuleResult(
                _rule("no-bak"),
                "fail",
                "2 forbidden files changed",
                [
                    Evidence("added forbidden file", path="src/a.bak", line=3, excerpt="x = 1"),
                    Evidence("ran it", event=17, excerpt="rm -rf build"),
                ],
            ),
            RuleResult(
                _rule("tests-run", check="require-command", source=None),
                "unverified",
                "exit code unknown",
                [Evidence("pytest ran", event=4, excerpt="pytest -q")],
            ),
            RuleResult(_rule("no-force-push", check="forbid-command"), "skip", "no transcript"),
            RuleResult(
                _rule("small-diff", check="max-diff", severity="warning", source="CLAUDE.md"),
                "fail",
                "diff too large",
            ),
            RuleResult(_rule("passes-2"), "pass", "fine"),
        ],
        notes=["no transcript: 1 transcript rule skipped"],
    )


# --------------------------------------------------------------------------- text


def test_text_groups_failures_first_and_formats_evidence() -> None:
    out = text.render(make_report(), color=False)
    lines = out.splitlines()
    first = {
        key: next(i for i, ln in enumerate(lines) if key in ln)
        for key in ("no-bak", "tests-run", "no-force-push", "passes-1")
    }
    assert first["no-bak"] < first["tests-run"] < first["no-force-push"] < first["passes-1"]
    assert "\u2717 no-bak  2 forbidden files changed" in out
    assert "    src/a.bak:3  added forbidden file: x = 1" in out
    assert "    #17  ran it: rm -rf build" in out
    assert "    rule: description of no-bak (AGENTS.md:12)" in out
    assert "rule: description of tests-run (ruleproof.toml:3)" in out  # falls back to origin
    assert "small-diff [warning]" in out
    assert "no-force-push  skipped: no transcript" in out
    assert "6 rules: 2 failed, 1 unverified, 1 skipped, 2 passed" in out
    assert out.rstrip().endswith("note: no transcript: 1 transcript rule skipped")
    assert "\x1b" not in out


def test_text_quiet_collapses_passes() -> None:
    out = text.render(make_report(), color=False, quiet=True)
    assert "passes-1" not in out
    assert "\u2713 2 rules passed" in out


def test_text_color_and_ascii() -> None:
    colored = text.render(make_report(), color=True)
    assert "\x1b[31m" in colored and "\x1b[0m" in colored
    plain = text.render(make_report(), color=False, unicode=False)
    assert plain.isascii()
    assert "x no-bak" in plain and "+ passes-1" in plain


def test_text_strips_terminal_escapes_from_evidence() -> None:
    report = Report(
        kind="check",
        tool_version="1",
        repo=None,
        results=[
            RuleResult(
                _rule("r"),
                "fail",
                "bad\nthing",
                [Evidence("m", event=1, excerpt="\x1b[2Jboom\x07")],
            )
        ],
    )
    out = text.render(report, color=False)
    assert "\x1b" not in out and "\x07" not in out
    assert "bad thing" in out and "#1  m: boom" in out


def test_text_empty_report() -> None:
    out = text.render(Report(kind="check", tool_version="1", repo=None), color=False)
    assert out == "0 rules: 0 failed, 0 unverified, 0 skipped, 0 passed\n"
    out = text.render(Report(kind="doctor", tool_version="1", repo=None), color=False)
    assert out == "no findings\n"


# --------------------------------------------------------------------------- json


def test_json_schema_and_key_order() -> None:
    out = json_report.render(make_report())
    data = json.loads(out)
    assert list(data) == [
        "schema",
        "kind",
        "version",
        "repo",
        "base",
        "session",
        "summary",
        "results",
        "notes",
    ]
    assert data["schema"] == "ruleproof/report@1"
    assert data["version"] == "9.9.9"
    assert data["session"] == {"agent": "claude-code", "id": "abc123", "path": "/t/abc123.jsonl"}
    assert data["summary"] == {"total": 6, "failed": 2, "unverified": 1, "skipped": 1, "passed": 2}
    first = data["results"][1]
    assert list(first) == [
        "id",
        "check",
        "status",
        "severity",
        "summary",
        "description",
        "source",
        "origin",
        "evidence",
    ]
    assert first["evidence"][0] == {
        "message": "added forbidden file",
        "path": "src/a.bak",
        "line": 3,
        "event": None,
        "excerpt": "x = 1",
    }
    assert out == json_report.render(make_report())  # deterministic


def test_json_without_session() -> None:
    data = json.loads(json_report.render(Report(kind="doctor", tool_version="1", repo=None)))
    assert data["session"] is None and data["results"] == [] and data["kind"] == "doctor"


# --------------------------------------------------------------------------- markdown


def test_markdown_structure() -> None:
    out = markdown.render(make_report())
    assert out.startswith("### ruleproof: 2 failed\n")
    assert "| Status | Rule | Severity | Summary | Source |" in out
    assert "| fail | `no-bak` | error | 2 forbidden files changed | `AGENTS.md:12` |" in out
    assert "| unverified | `tests-run` | error | exit code unknown |  |" in out
    assert "<details><summary><code>no-bak</code>: 2 forbidden files changed</summary>" in out
    assert "src/a.bak:3  added forbidden file: x = 1" in out
    assert "<details><summary>2 passed</summary>" in out
    assert "<details><summary>1 skipped</summary>" in out
    assert "> no transcript: 1 transcript rule skipped" in out


def test_markdown_escaping() -> None:
    rule = _rule("weird`id|x", source="docs/A|B.md:3")
    report = Report(
        kind="check",
        tool_version="1",
        repo=None,
        results=[
            RuleResult(
                rule,
                "fail",
                "a | b <script>alert(1)</script> *bold*",
                [Evidence("m", path="p.py", line=1, excerpt="```\n</details>")],
            )
        ],
    )
    out = markdown.render(report)
    row = next(ln for ln in out.splitlines() if ln.startswith("| fail"))
    assert "``weird`id\\|x``" in row
    assert "a \\| b &lt;script&gt;alert(1)&lt;/script&gt; \\*bold\\*" in row
    assert "`docs/A\\|B.md:3`" in row
    assert row.count(" | ") == 4  # every literal pipe is escaped
    assert "<script>" not in out
    assert "<code>weird`id|x</code>" in out
    assert "````text" in out  # fence longer than the backticks in the evidence


def test_markdown_all_passed() -> None:
    report = Report(
        kind="doctor", tool_version="1", repo=None, results=[RuleResult(_rule("a"), "pass", "ok")]
    )
    out = markdown.render(report)
    assert out.startswith("### ruleproof doctor: all rules passed\n")
    assert "| Status" not in out


def test_markdown_code_span_helpers() -> None:
    assert markdown.code_span("a`b") == "``a`b``"
    assert markdown.code_span("`a") == "`` `a ``"
    assert markdown.code_span("a|b") == "`a|b`"
    assert markdown.code_span("a|b", table=True) == "`a\\|b`"


# --------------------------------------------------------------------------- sarif


def test_sarif_required_properties() -> None:
    doc = json.loads(sarif.render(make_report()))
    assert doc["version"] == "2.1.0"
    assert doc["$schema"].endswith("sarif-2.1.0.json")
    assert isinstance(doc["runs"], list) and len(doc["runs"]) == 1
    run = doc["runs"][0]
    driver = run["tool"]["driver"]
    assert driver["name"] == "ruleproof"
    assert driver["version"] == "9.9.9"
    assert driver["informationUri"] == PROJECT_URL
    ids = [r["id"] for r in driver["rules"]]
    assert len(ids) == len(set(ids)) == 6
    for descriptor in driver["rules"]:
        assert descriptor["shortDescription"]["text"]
        assert descriptor["help"]["text"]
    no_bak = driver["rules"][ids.index("no-bak")]
    assert "Source: AGENTS.md:12" in no_bak["help"]["text"]

    results = {r["ruleId"]: r for r in run["results"]}
    assert set(results) == {"no-bak", "tests-run", "small-diff"}
    for res in run["results"]:
        assert res["message"]["text"]
        assert ids[res["ruleIndex"]] == res["ruleId"]
        assert res["level"] in ("none", "note", "warning", "error")
        for loc in res.get("locations", []) + res.get("relatedLocations", []):
            phys = loc["physicalLocation"]
            assert phys["artifactLocation"]["uri"]
            if "region" in phys:
                assert phys["region"]["startLine"] >= 1

    assert results["no-bak"]["level"] == "error"
    assert results["no-bak"]["locations"][0]["physicalLocation"] == {
        "artifactLocation": {"uri": "src/a.bak"},
        "region": {"startLine": 3},
    }
    assert results["tests-run"]["level"] == "note"  # unverified
    assert "locations" not in results["tests-run"]  # no file evidence and no source
    # transcript-only evidence: points at the instruction file, no line for "CLAUDE.md"
    assert results["small-diff"]["level"] == "warning"
    assert results["small-diff"]["locations"][0]["physicalLocation"] == {
        "artifactLocation": {"uri": "CLAUDE.md"}
    }
    assert run["properties"]["notes"] == ["no transcript: 1 transcript rule skipped"]


def test_sarif_points_at_source_line_for_transcript_evidence() -> None:
    report = Report(
        kind="check",
        tool_version="1",
        repo=None,
        results=[
            RuleResult(
                _rule("r", severity="info", source="pkg/AGENTS.md:7"),
                "fail",
                "s",
                [Evidence("ran", event=3)],
            )
        ],
    )
    res = sarif.to_sarif(report)["runs"][0]["results"][0]
    assert res["level"] == "note"
    assert res["locations"][0]["physicalLocation"] == {
        "artifactLocation": {"uri": "pkg/AGENTS.md"},
        "region": {"startLine": 7},
    }


def test_sarif_related_locations_and_uri_quoting() -> None:
    evidence = [Evidence("a", path="my dir/a.py", line=1), Evidence("b", path="b.py", line=2)]
    report = Report(
        kind="check",
        tool_version="1",
        repo=None,
        results=[RuleResult(_rule("r"), "fail", "s", evidence)],
    )
    res = sarif.to_sarif(report)["runs"][0]["results"][0]
    assert res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "my%20dir/a.py"
    assert res["relatedLocations"][0]["id"] == 1
    assert res["relatedLocations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "b.py"


# --------------------------------------------------------------------------- dispatcher


@pytest.mark.parametrize("fmt", FORMATS)
def test_dispatcher_renders_every_format(fmt: str) -> None:
    out = render(make_report(), fmt, color=False)
    assert out.endswith("\n") and "no-bak" in out


def test_dispatcher_rejects_unknown_format() -> None:
    with pytest.raises(ConfigError, match="unknown report format"):
        render(make_report(), "xml")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("AGENTS.md:12", ("AGENTS.md", 12)),
        ("AGENTS.md", ("AGENTS.md", None)),
        ("C:/x/AGENTS.md:3", ("C:/x/AGENTS.md", 3)),
        ("AGENTS.md:0", ("AGENTS.md:0", None)),
        (None, None),
        ("", None),
    ],
)
def test_split_source(source: str | None, expected: tuple[str, int | None] | None) -> None:
    assert split_source(source) == expected
