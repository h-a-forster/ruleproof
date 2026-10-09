"""Build a ``Diff`` from a git working tree or from a unified diff (patch) text."""

from __future__ import annotations

import difflib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ruleproof.errors import GitError
from ruleproof.models import ChangeStatus, Diff, FileChange
from ruleproof.paths import normalize

UNTRACKED_MAX_BYTES = 1024 * 1024
"""Untracked files larger than this are reported as binary, without content."""

_BINARY_SNIFF_BYTES = 8192

_GIT_DIFF_OPTS = (
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--no-relative",
    "--submodule=short",  # diff.submodule=log|diff would hide gitlink changes
    "-M",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_BINARY_RE = re.compile(r"^Binary files (.+) and (.+) differ$")
_STATUS: dict[str, ChangeStatus] = {"A": "added", "D": "deleted"}  # M, T, U, X: modified
_REPO_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
)
_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


# --------------------------------------------------------------------------- git


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    env = {k: v for k, v in os.environ.items() if k not in _REPO_ENV_VARS}  # use `repo` only
    env["GIT_OPTIONAL_LOCKS"] = "0"  # read-only: never contend for index.lock
    try:
        return subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=repo,
            input=stdin,
            capture_output=True,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH") from None
    except NotADirectoryError:
        raise GitError(f"not a directory: {repo}") from None


def _git_ok(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    proc = _git(repo, *args, stdin=stdin)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} failed in {repo}: {err or f'exit {proc.returncode}'}")
    return proc.stdout


def repo_root(path: Path) -> Path:
    """Top-level directory of the git work tree containing ``path``."""
    if not path.is_dir():
        raise GitError(f"not a directory: {path}")
    proc = _git(path, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        raise GitError(
            f"{path} is not inside a git repository; run `git init`, pass --repo DIR, "
            "or use --patch FILE / --no-diff"
        )
    return Path(proc.stdout.decode("utf-8", "replace").strip())


def _resolve(repo: Path, ref: str) -> str | None:
    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return proc.stdout.decode().strip() if proc.returncode == 0 else None


def _base_tree(repo: Path, base: str) -> str:
    """The commit (or empty tree) that the working tree is compared against."""
    if base.startswith("-"):
        raise GitError(f"invalid git ref {base!r}")
    commit = _resolve(repo, base)
    if commit is None:
        if base == "HEAD":  # unborn branch: everything in the index and work tree is new
            return _git_ok(repo, "hash-object", "-t", "tree", "--stdin", stdin=b"").decode().strip()
        if re.fullmatch(r"(?:HEAD|@)(?:[~^]\d*)+", base):
            raise GitError(
                f"{base!r} does not exist in {repo}: the history is too short (shallow clone "
                "or too few commits); fetch more with `git fetch --deepen=N` or use another base"
            )
        hint = ""
        if re.fullmatch(r"[\w./-]+", base):
            hint = f" (e.g. `git fetch origin {base.removeprefix('origin/')}`)"
        raise GitError(f"unknown git ref {base!r} in {repo}; check the name, or fetch it{hint}")
    if base == "HEAD":
        return commit
    proc = _git(repo, "merge-base", commit, "HEAD")
    if proc.returncode == 0:
        return proc.stdout.decode().strip()
    shallow = _git(repo, "rev-parse", "--is-shallow-repository").stdout.strip() == b"true"
    if shallow:
        raise GitError(
            f"no common ancestor of {base!r} and HEAD in {repo}: the clone is shallow, so "
            "upstream changes would be reported as yours. Fetch more history with "
            "`git fetch --deepen=100` (or `git fetch --unshallow`); in GitHub Actions set "
            "`fetch-depth: 0` on actions/checkout"
        )
    return commit  # unborn HEAD or genuinely unrelated histories: compare with base itself


def from_git(repo: Path, base: str = "HEAD", include_untracked: bool = True) -> Diff:
    """Changes in the working tree of ``repo`` relative to ``base``.

    Staged and unstaged changes to tracked files are included (``git diff <base>``), plus
    untracked, non-ignored files as ``added`` when ``include_untracked``.

    ``base`` is a single ref. When it is not ``HEAD`` it is first resolved through
    ``git merge-base <base> HEAD``, so ``base="origin/main"`` shows what this branch changed
    (as a pull request would) and excludes commits that landed upstream since it forked.
    Genuinely unrelated histories (no common ancestor in a full clone) fall back to comparing
    with ``base`` itself; in a shallow clone a missing merge base is an error, because the
    comparison would attribute upstream changes to this branch. In a repository with no
    commits, ``HEAD`` compares against the empty tree.

    A path that is deleted from the index but still on disk (``git rm --cached``) is reported
    by its content: unchanged if it equals the base, else ``modified``.

    Raises ``GitError`` when git is missing, ``repo`` is not a repository, ``base`` is
    unknown, or a shallow clone lacks the merge base.
    """
    root = repo_root(repo)
    tree = _base_tree(root, base)

    status_out = _git_ok(root, "diff", *_GIT_DIFF_OPTS, "--name-status", "-z", tree, "--")
    patch_out = _git_ok(root, "diff", *_GIT_DIFF_OPTS, "-U0", tree, "--")

    sections: dict[str, _Section] = {}
    for section in _parse_sections(_decode(patch_out)):
        key = section.path()
        if key is None:
            continue
        prev = sections.get(key)
        if prev is None:
            sections[key] = section
        elif prev.status() == "deleted":  # type change (file <-> symlink): delete + add
            section.removed += prev.removed
            sections[key] = section

    files: list[FileChange] = []
    for status, path, old_path in _parse_name_status(status_out):
        change = FileChange(path=path, status=status, old_path=old_path)
        sec = sections.get(path)
        if sec is not None:
            change.binary = sec.binary
            if status != "deleted":
                change.added = sec.added
            change.removed = sec.removed
        files.append(change)

    if include_untracked:
        deleted = {f.path: f for f in files if f.status == "deleted"}
        out = _git_ok(root, "ls-files", "--others", "--exclude-standard", "-z")
        for raw in out.split(b"\0"):
            if not raw or raw.endswith(b"/"):
                continue
            rel = _decode(raw)
            gone = deleted.get(rel)
            if gone is None:
                files.append(_untracked(root, rel))
                continue
            files.remove(gone)  # untracked but still on disk: compare its content with base
            kept = _against_base(root, tree, rel)
            if kept is not None:
                files.append(kept)

    files.sort(key=lambda f: f.path)
    return Diff(base=base, files=files)


def _parse_name_status(out: bytes) -> list[tuple[ChangeStatus, str, str | None]]:
    fields = [_decode(f) for f in out.split(b"\0")]
    result: list[tuple[ChangeStatus, str, str | None]] = []
    i = 0
    while i < len(fields) and fields[i]:
        code = fields[i][:1]
        if code in ("R", "C"):
            old, new = fields[i + 1], fields[i + 2]
            result.append(("renamed", new, old) if code == "R" else ("added", new, None))
            i += 3
            continue
        path = fields[i + 1]
        result.append((_STATUS.get(code, "modified"), path, None))
        i += 2
    return result


def _against_base(root: Path, tree: str, rel: str) -> FileChange | None:
    """``rel`` (on disk) compared with its blob in ``tree``; None when identical."""
    old = _git_ok(root, "cat-file", "blob", f"{tree}:{rel}")
    try:
        new = (root / rel).read_bytes()
    except OSError:
        return FileChange(path=rel, status="modified", binary=True)
    if old == new:
        return None
    change = FileChange(path=rel, status="modified")
    sniff = _BINARY_SNIFF_BYTES
    if b"\0" in old[:sniff] or b"\0" in new[:sniff] or len(new) > UNTRACKED_MAX_BYTES:
        change.binary = True
        return change
    a, b = _lines(old), _lines(new)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            change.removed += i2 - i1
        if tag in ("replace", "insert"):
            change.added.extend((j + 1, b[j]) for j in range(j1, j2))
    return change


def _lines(data: bytes) -> list[str]:
    lines = _decode(data).split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def _untracked(root: Path, rel: str) -> FileChange:
    change = FileChange(path=rel, status="added")
    full = root / rel
    try:
        if full.is_symlink():
            change.added = [(1, os.readlink(full))]
            return change
        if full.stat().st_size > UNTRACKED_MAX_BYTES:
            change.binary = True
            return change
        data = full.read_bytes()
    except OSError:
        change.binary = True  # unreadable: report the file, not its content
        return change
    if b"\0" in data[:_BINARY_SNIFF_BYTES]:
        change.binary = True
        return change
    change.added = list(enumerate(_lines(data), 1))
    return change


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "replace")


# ------------------------------------------------------------------------- patch


@dataclass(slots=True)
class _Section:
    """One file's part of a unified diff."""

    git: bool = False
    header_old: str | None = None
    header_new: str | None = None
    old: str | None = None  # from ---/+++ or rename lines; None for /dev/null
    new: str | None = None
    have_markers: bool = False  # saw the ---/+++ pair
    new_file: bool = False
    deleted_file: bool = False
    renamed: bool = False
    copied: bool = False
    binary: bool = False
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: int = 0

    def status(self) -> ChangeStatus:
        if self.copied or self.new_file or (self.have_markers and self.old is None):
            return "added"
        if self.deleted_file or (self.have_markers and self.new is None):
            return "deleted"
        if self.renamed:
            return "renamed"
        return "modified"

    def old_path(self) -> str | None:
        return self.old if self.have_markers or self.renamed else self.header_old

    def new_path(self) -> str | None:
        return self.new if self.have_markers or self.renamed else self.header_new

    def path(self) -> str | None:
        if self.status() == "deleted":
            return self.old_path() or self.header_old
        return self.new_path() or self.header_new


def from_patch(text: str, base: str | None = None) -> Diff:
    """Parse a unified diff: ``git diff`` / ``git format-patch`` output or plain ``diff -u``.

    Paths are made repo-relative posix (``a/`` and ``b/`` prefixes stripped). Lines that are
    not part of a diff (mail headers, commit messages, ``Index:`` lines) are ignored.
    """
    files: list[FileChange] = []
    for sec in _parse_sections(text):
        path = sec.path()
        if path is None:
            continue
        status = sec.status()
        change = FileChange(
            path=normalize(path),
            status=status,
            old_path=normalize(sec.old_path() or "") if status == "renamed" else None,
            removed=sec.removed,
            binary=sec.binary,
        )
        if status != "deleted":
            change.added = sec.added
        files.append(change)
    return Diff(base=base, files=files)


def _parse_sections(text: str) -> list[_Section]:
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    sections: list[_Section] = []
    cur: _Section | None = None
    in_header = False  # inside a git section, before its first hunk
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            cur = _Section(git=True)
            cur.header_old, cur.header_new = _split_git_header(line[len("diff --git ") :])
            sections.append(cur)
            in_header = True
        elif line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or not cur.git or cur.have_markers:
                cur = _Section()
                sections.append(cur)
            old = _marker_path(line[4:])
            new = _marker_path(lines[i + 1][4:])
            if cur.git or ((old or "a/").startswith("a/") and (new or "b/").startswith("b/")):
                old = old[2:] if old and old.startswith("a/") else old
                new = new[2:] if new and new.startswith("b/") else new
            cur.old, cur.new, cur.have_markers = old, new, True
            in_header = False
            i += 2
            continue
        elif line.startswith("@@ ") and cur is not None:
            m = _HUNK_RE.match(line)
            if m:
                i = _read_hunk(lines, i + 1, m, cur)
                in_header = False
                continue
        elif cur is not None and cur.git and in_header:
            _git_extended_header(line, cur)
        elif line.startswith("Binary files "):
            m = _BINARY_RE.match(line)
            if m:
                cur = _Section(binary=True, have_markers=True)
                cur.old = _strip_prefix(_marker_path(m.group(1)), "a/")
                cur.new = _strip_prefix(_marker_path(m.group(2)), "b/")
                sections.append(cur)
        i += 1
    return sections


def _git_extended_header(line: str, cur: _Section) -> None:
    if line.startswith("new file mode"):
        cur.new_file = True
    elif line.startswith("deleted file mode"):
        cur.deleted_file = True
    elif line.startswith("rename from "):
        cur.renamed, cur.old = True, _unquote(line[len("rename from ") :])
    elif line.startswith("rename to "):
        cur.renamed, cur.new = True, _unquote(line[len("rename to ") :])
    elif line.startswith(("copy from ", "copy to ")):
        cur.copied = True
    elif line.startswith(("Binary files ", "GIT binary patch")):
        cur.binary = True


def _read_hunk(lines: list[str], i: int, m: re.Match[str], cur: _Section) -> int:
    """Consume the body of a hunk starting at ``lines[i]``; return the next line index."""
    old_left = int(m.group(2)) if m.group(2) is not None else 1
    new_left = int(m.group(4)) if m.group(4) is not None else 1
    new_no = int(m.group(3))
    n = len(lines)
    while i < n and (old_left > 0 or new_left > 0):
        line = lines[i]
        tag = line[:1]
        if tag == "+":
            cur.added.append((new_no, line[1:]))
            new_no += 1
            new_left -= 1
        elif tag == "-":
            cur.removed += 1
            old_left -= 1
        elif tag in (" ", ""):  # some tools strip the space of empty context lines
            new_no += 1
            old_left -= 1
            new_left -= 1
        elif tag != "\\":  # "\ No newline at end of file"
            break
        i += 1
    while i < n and lines[i].startswith("\\"):
        i += 1
    return i


def _marker_path(raw: str) -> str | None:
    """Path from a ``---``/``+++`` line (after the marker); None for /dev/null."""
    if raw.startswith('"'):
        path, _ = _read_quoted(raw)
    else:
        path = raw.split("\t", 1)[0]  # plain diffs append "\t<timestamp>"
    return None if path == "/dev/null" else path


def _strip_prefix(path: str | None, prefix: str) -> str | None:
    return path[len(prefix) :] if path and path.startswith(prefix) else path


def _split_git_header(rest: str) -> tuple[str | None, str | None]:
    """Split ``a/old b/new`` from a ``diff --git`` line, honouring quoted paths."""
    if rest.startswith('"'):
        old, tail = _read_quoted(rest)
        new_raw = tail.lstrip(" ")
        new = _read_quoted(new_raw)[0] if new_raw.startswith('"') else new_raw
    elif ' "' in rest and rest.endswith('"'):
        k = rest.index(' "')
        old, new = rest[:k], _read_quoted(rest[k + 1 :])[0]
    else:
        mid = len(rest) // 2  # "a/P b/P": same path on both sides is by far the common case
        if len(rest) % 2 == 1 and rest[mid] == " " and rest[2:mid] == rest[mid + 3 :]:
            old, new = rest[:mid], rest[mid + 1 :]
        elif " b/" in rest:
            k = rest.index(" b/")
            old, new = rest[:k], rest[k + 1 :]
        else:
            return None, None
    return _strip_prefix(old, "a/"), _strip_prefix(new, "b/")


def _unquote(raw: str) -> str:
    return _read_quoted(raw)[0] if raw.startswith('"') else raw


def _read_quoted(raw: str) -> tuple[str, str]:
    """Decode a git C-style quoted string at the start of ``raw``; return (value, rest)."""
    out = bytearray()
    i, n = 1, len(raw)
    while i < n:
        c = raw[i]
        if c == '"':
            return _decode(bytes(out)), raw[i + 1 :]
        if c == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt in _C_ESCAPES:
                out.append(_C_ESCAPES[nxt])
                i += 2
                continue
            octal = raw[i + 1 : i + 4]
            if len(octal) == 3 and all(ch in "01234567" for ch in octal):
                out.append(int(octal, 8) & 0xFF)
                i += 4
                continue
        out.extend(c.encode("utf-8"))
        i += 1
    return _decode(bytes(out)), ""  # unterminated: take what we have
