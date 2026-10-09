from __future__ import annotations

import base64

import pytest

from ruleproof.transcripts._codemode import parse_cell, patch_files, scan
from ruleproof.transcripts._shell import truncate, unwrap

BS = chr(92)  # a backslash, spelled out to keep escapes readable below
NL = BS + "n"  # a JavaScript newline escape


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (["bash", "-lc", "pytest -q"], "pytest -q"),
        (["/bin/zsh", "-c", "npm test"], "npm test"),
        (["sh", "-o", "pipefail", "-c", "make | tee log"], "make | tee log"),
        ("bash -lc 'echo \"hi\" && ls'", 'echo "hi" && ls'),
        ('/bin/zsh -c "npm test"', "npm test"),
        ("sh -c ls", "ls"),
        ('powershell.exe -NoProfile -Command "Get-ChildItem; pytest"', "Get-ChildItem; pytest"),
        (["pwsh.exe", "-NoProfile", "-Command", "Get-Location"], "Get-Location"),
        ("pwsh -c git status", "git status"),
        ("powershell -ExecutionPolicy Bypass -c git status", "git status"),
        ("powershell git status", "git status"),
        ("cmd /c dir /b", "dir /b"),
        ('cmd.exe /S /C "echo hi"', "echo hi"),
        ('bash -lc "powershell -Command ls"', "ls"),
        ("pytest -q", "pytest -q"),
        ("bash script.sh", "bash script.sh"),
        ("pwsh -File build.ps1", "pwsh -File build.ps1"),
        ("sh -c", "sh -c"),
        (["git", "commit", "-m", "two words"], 'git commit -m "two words"'),
    ],
)
def test_unwrap(command: str | list[str], expected: str) -> None:
    assert unwrap(command) == expected


def test_unwrap_windows_exe_path_and_encoded_command() -> None:
    exe = f'"C:{BS}Program Files{BS}PowerShell{BS}7{BS}pwsh.exe"'
    assert unwrap(f'{exe} -Command "git log"') == "git log"
    encoded = base64.b64encode("Write-Output 1".encode("utf-16-le")).decode()
    assert unwrap(["powershell", "-EncodedCommand", encoded]) == "Write-Output 1"


def test_truncate_keeps_head_and_tail() -> None:
    assert truncate("short", 10) == "short"
    out = truncate("A" * 50 + "B" * 50, 60)
    assert len(out) <= 60
    assert out.startswith("A") and out.endswith("B")
    assert "truncated" in out


def test_patch_files() -> None:
    patch = (
        "*** Begin Patch\r\n*** Add File: a.py\r\n+x\r\n*** Update File: b.py\r\n"
        "*** Move to: c.py\r\n@@\r\n*** Delete File: d.py\r\n*** Update File: e.py\r\n*** End Patch"
    )
    assert patch_files(patch) == [
        ("add", "a.py"),
        ("delete", "b.py"),
        ("add", "c.py"),
        ("delete", "d.py"),
        ("modify", "e.py"),
    ]


def test_scan_masks_strings_and_comments() -> None:
    source = 'a("x)") // tools.y(\n/* tools.z( */ b'
    masked, literals = scan(source)
    assert len(masked) == len(source)
    assert "tools" not in masked
    assert [lit.value for lit in literals] == ["x)"]


def test_parse_cell_decodes_js_escapes() -> None:
    js = (
        "await tools.exec_command({cmd: "
        f'"echo {BS}"a{BS}" {BS}x41{BS}u0042{BS}u{{43}}{BS}tend"'
        "});"
    )
    (call,) = parse_cell(js).calls
    assert call.command == 'echo "a" ABC\tend'


def test_parse_cell_call_forms() -> None:
    js = """
const where = "C:/repo";
const list = 'git diff';
await Promise.allSettled([
  tools.exec_command({cmd: `uv run pytest -q`, workdir: where}),
  tools.exec_command({ "cmd": list }),
  tools.exec_command({cmd: cmds[0]}),
  tools.mcp__node_repl__js({code: "1 + 1"}),
]);
const s = "tools.exec_command({cmd: 'not a call'})";
mytools.exec_command({cmd: "not tools"});
"""
    calls = parse_cell(js).calls
    assert [(c.tool, c.command) for c in calls] == [
        ("exec_command", "uv run pytest -q"),
        ("exec_command", "git diff"),
        ("exec_command", None),
        ("mcp__node_repl__js", None),
    ]
    assert calls[0].workdir == "C:/repo"


def test_parse_cell_patch_only_when_apply_patch_called() -> None:
    patch = "*** Begin Patch" + NL + "*** Update File: x.py" + NL + "*** End Patch"
    (call,) = parse_cell(f'await tools.apply_patch("{patch}");').calls
    assert call.patch is not None and patch_files(call.patch) == [("modify", "x.py")]
    assert parse_cell(f'const p = "{patch}";').calls == []
