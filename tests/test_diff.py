from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from ruleproof.diff import from_git, from_patch, repo_root
from ruleproof.errors import GitError
from ruleproof.models import Diff, FileChange

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# ----------------------------------------------------------------------------- helpers


def by_path(diff: Diff) -> dict[str, FileChange]:
    return {f.path: f for f in diff.files}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    with (path / ".git" / "config").open("a", encoding="utf-8") as fh:  # saves three `git config`
        fh.write("[user]\n\tname = Test\n\temail = test@example.invalid\n")
        fh.write("[core]\n\tautocrlf = false\n")
    return path


def commit_all(repo: Path, msg: str = "commit") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


@pytest.fixture(scope="module")
def isolated_git(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Keep the user's and the system's git config out of the tests."""
    home = tmp_path_factory.mktemp("home")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GIT_CONFIG_NOSYSTEM", "1")
        mp.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
        mp.delenv("GIT_DIR", raising=False)
        mp.delenv("GIT_WORK_TREE", raising=False)
        yield home


@pytest.fixture(scope="module")
def worktree(tmp_path_factory: pytest.TempPathFactory, isolated_git: Path) -> Path:
    """A repo with one commit and every kind of working-tree change on top."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = init_repo(tmp_path_factory.mktemp("wt") / "repo")
    (repo / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    (repo / "keep.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (repo / "gone.txt").write_text("a\nb\n", encoding="utf-8")
    (repo / "old name.txt").write_text("".join(f"line {i}\n" for i in range(20)), encoding="utf-8")
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    (repo / "script.sh").write_text("echo hi\n", encoding="utf-8")
    (repo / "crlf.txt").write_bytes(b"x\r\n")
    (repo / "noeol.txt").write_bytes(b"first\nlast")
    (repo / "uncached same.txt").write_text("kept\n", encoding="utf-8")
    (repo / "uncached edited.txt").write_text("a\nb\nc\n", encoding="utf-8")
    commit_all(repo, "initial")
    with (repo / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write("[diff]\n\tsubmodule = log\n")  # must not hide gitlink changes

    (repo / "keep.txt").write_text("one\nTWO\nthree\nfour\n", encoding="utf-8")  # unstaged
    (repo / "gone.txt").unlink()
    git(repo, "mv", "old name.txt", "new name.txt")
    with (repo / "new name.txt").open("a", encoding="utf-8") as fh:
        fh.write("appended\n")
    (repo / "blob.bin").write_bytes(b"\x00\x09\x09")
    git(repo, "update-index", "--chmod=+x", "script.sh")  # mode-only change
    (repo / "script.sh").chmod(0o755)  # where core.fileMode is on, the work tree decides
    (repo / "crlf.txt").write_bytes(b"x\r\ny\r\n")
    (repo / "noeol.txt").write_bytes(b"first\nlast\nmore")
    (repo / "staged.py").write_text("print('staged')\n", encoding="utf-8")
    git(repo, "add", "staged.py", "keep.txt")
    git(repo, "rm", "-q", "--cached", "uncached same.txt", "uncached edited.txt")
    (repo / "uncached edited.txt").write_text("a\nB\nc\nd\n", encoding="utf-8")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},sub")
    (repo / "sub").mkdir()  # an unpopulated submodule checkout
    (repo / "keep.txt").write_text("one\nTWO\nthree\nfour\nfive\n", encoding="utf-8")

    (repo / "dir with space").mkdir()
    (repo / "dir with space" / "ünïcode.md").write_text("héllo\n", encoding="utf-8")
    (repo / "latin1.txt").write_bytes(b"caf\xe9\n")
    (repo / "untracked.bin").write_bytes(b"abc\x00def")
    (repo / "big.txt").write_bytes(b"x" * (1024 * 1024 + 1))
    (repo / "ignored.log").write_text("noise\n", encoding="utf-8")
    return repo


@pytest.fixture(scope="module")
def worktree_diff(worktree: Path) -> Diff:
    return from_git(worktree)


# -------------------------------------------------------------------------------- git


@needs_git
def test_from_git_tracked_changes(worktree_diff: Diff) -> None:
    files = by_path(worktree_diff)

    keep = files["keep.txt"]
    assert keep.status == "modified"
    assert keep.added == [(2, "TWO"), (4, "four"), (5, "five")]  # staged + unstaged together
    assert keep.removed == 1

    assert files["gone.txt"].status == "deleted"
    assert files["gone.txt"].added == []
    assert files["gone.txt"].removed == 2

    renamed = files["new name.txt"]
    assert renamed.status == "renamed"
    assert renamed.old_path == "old name.txt"
    assert renamed.added == [(21, "appended")]

    assert files["blob.bin"].binary is True
    assert files["blob.bin"].added == []

    mode = files["script.sh"]
    assert (mode.status, mode.added, mode.removed) == ("modified", [], 0)

    assert files["crlf.txt"].added == [(2, "y")]
    noeol = files["noeol.txt"]
    assert noeol.added == [(2, "last"), (3, "more")]
    assert noeol.removed == 1

    assert files["staged.py"].status == "added"

    # `git rm --cached`: still on disk, so judged by content, never deleted + added
    assert "uncached same.txt" not in files
    edited = files["uncached edited.txt"]
    assert (edited.status, edited.added, edited.removed) == ("modified", [(2, "B"), (4, "d")], 1)

    sub = files["sub"]
    assert (sub.status, sub.added) == ("added", [(1, f"Subproject commit {'1' * 40}")])
    assert files["staged.py"].added == [(1, "print('staged')")]


@needs_git
def test_from_git_untracked_files(worktree_diff: Diff) -> None:
    assert worktree_diff.base == "HEAD"
    files = by_path(worktree_diff)

    uni = files["dir with space/ünïcode.md"]
    assert (uni.status, uni.added) == ("added", [(1, "héllo")])
    assert files["latin1.txt"].added == [(1, "caf�")]
    assert files["untracked.bin"].binary is True
    assert files["untracked.bin"].added == []
    assert files["big.txt"].binary is True
    assert "ignored.log" not in files
    assert ".gitignore" not in files  # committed, unchanged


@needs_git
def test_from_git_subdirectory_without_untracked(worktree: Path) -> None:
    paths = from_git(worktree / "dir with space", include_untracked=False).paths()
    assert "latin1.txt" not in paths
    assert "keep.txt" in paths  # paths stay relative to the repo root
    assert paths == sorted(paths)
    assert repo_root(worktree / "dir with space").resolve() == worktree.resolve()


@needs_git
def test_from_git_unknown_ref(worktree: Path) -> None:
    with pytest.raises(GitError, match="unknown git ref 'no-such-branch'"):
        from_git(worktree, base="no-such-branch")
    with pytest.raises(GitError, match="invalid git ref"):
        from_git(worktree, base="--output=x")


@needs_git
def test_from_git_not_a_repo(tmp_path: Path, isolated_git: Path) -> None:
    with pytest.raises(GitError, match="not inside a git repository"):
        from_git(tmp_path)
    with pytest.raises(GitError, match="not a directory"):
        repo_root(tmp_path / "missing")


def test_git_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(GitError, match="git is not installed"):
        repo_root(tmp_path)


@needs_git
def test_from_git_no_commits(tmp_path: Path, isolated_git: Path) -> None:
    repo = init_repo(tmp_path / "fresh")
    (repo / "a.txt").write_text("staged\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    (repo / "b.txt").write_text("untracked\n", encoding="utf-8")

    diff = from_git(repo)
    files = by_path(diff)
    assert diff.base == "HEAD"
    assert files["a.txt"].status == "added"
    assert files["a.txt"].added == [(1, "staged")]
    assert files["b.txt"].added == [(1, "untracked")]


def fast_import_commit(branch: str, message: str, files: dict[str, str], parent: str = "") -> str:
    """One commit in `git fast-import` syntax (builds history without a git call per commit)."""

    def data(text: str) -> str:
        return f"data {len(text.encode())}\n{text}\n"

    out = f"commit refs/heads/{branch}\ncommitter T <t@example.invalid> 0 +0000\n{data(message)}"
    if parent:
        out += f"from {parent}\n"
    for name, content in files.items():
        out += f"M 100644 inline {name}\n{data(content)}"
    return out


@needs_git
def test_from_git_base_uses_merge_base(tmp_path: Path, isolated_git: Path) -> None:
    repo = init_repo(tmp_path / "branches")
    stream = (
        fast_import_commit("main", "base", {"shared.txt": "base\n"})
        + fast_import_commit("feature", "mine", {"feature.txt": "mine\n"}, parent="refs/heads/main")
        + fast_import_commit("main", "upstream", {"upstream.txt": "theirs\n"})
    )
    subprocess.run(["git", "fast-import", "--quiet"], cwd=repo, input=stream.encode(), check=True)
    git(repo, "checkout", "-q", "-f", "feature")

    diff = from_git(repo, base="main")
    assert diff.base == "main"
    assert diff.paths() == ["feature.txt"]  # upstream.txt landed on main after the fork


# ------------------------------------------------------------------------------ patch

GIT_PATCH = """\
From 1234 Mon Sep 17 00:00:00 2001
Subject: [PATCH] example

diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -3 +3,2 @@ def main():
-    return 1
+    return 2
+--- not a header
@@ -10,0 +12 @@
+# tail
\\ No newline at end of file
diff --git a/new file.txt b/new file.txt
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new file.txt\t
@@ -0,0 +1,2 @@
+hello
+
diff --git a/old.txt b/old.txt
deleted file mode 100644
index 4444444..0000000
--- a/old.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-x
-y
diff --git a/before.py b/after.py
similarity index 100%
rename from before.py
rename to after.py
diff --git a/img.png b/img.png
index 5555555..6666666 100644
Binary files a/img.png and b/img.png differ
diff --git a/run.sh b/run.sh
old mode 100644
new mode 100755
diff --git "a/caf\\303\\251 \\"q\\".txt" "b/caf\\303\\251 \\"q\\".txt"
--- "a/caf\\303\\251 \\"q\\".txt"
+++ "b/caf\\303\\251 \\"q\\".txt"
@@ -1 +1 @@
-a
+b
diff --git a/empty.txt b/empty.txt
new file mode 100644
index 0000000..e69de29
"""


def test_from_patch_git_format() -> None:
    diff = from_patch(GIT_PATCH, base="main")
    assert diff.base == "main"
    files = by_path(diff)
    assert list(files) == [
        "src/app.py",
        "new file.txt",
        "old.txt",
        "after.py",
        "img.png",
        "run.sh",
        'café "q".txt',
        "empty.txt",
    ]

    app = files["src/app.py"]
    assert app.status == "modified"
    assert app.added == [(3, "    return 2"), (4, "--- not a header"), (12, "# tail")]
    assert app.removed == 1

    new = files["new file.txt"]
    assert (new.status, new.added) == ("added", [(1, "hello"), (2, "")])

    old = files["old.txt"]
    assert (old.status, old.added, old.removed) == ("deleted", [], 2)

    ren = files["after.py"]
    assert (ren.status, ren.old_path, ren.added) == ("renamed", "before.py", [])

    assert files["img.png"].binary is True
    assert files["run.sh"].status == "modified"
    assert files['café "q".txt'].added == [(1, "b")]
    assert files["empty.txt"].status == "added"


def test_from_patch_crlf() -> None:
    files = by_path(from_patch(GIT_PATCH.replace("\n", "\r\n")))
    assert files["src/app.py"].added[0] == (3, "    return 2")
    assert files["new file.txt"].added == [(1, "hello"), (2, "")]


def test_from_patch_plain_diff() -> None:
    text = """\
--- a/lib/x.c\t2024-01-01 00:00:00.000000000 +0000
+++ b/lib/x.c\t2024-01-02 00:00:00.000000000 +0000
@@ -1,3 +1,3 @@
 int a;
-int b;
+int c;
 int d;
--- /dev/null
+++ b/lib/y.c
@@ -0,0 +1 @@
+new
--- orig/z.txt
+++ z.txt
@@ -1 +1 @@
-old
+new
Binary files a/pic.gif and b/pic.gif differ
"""
    files = by_path(from_patch(text))
    assert files["lib/x.c"].added == [(2, "int c;")]
    assert files["lib/x.c"].removed == 1
    assert files["lib/y.c"].status == "added"
    assert files["z.txt"].status == "modified"  # differing old name in a plain diff is no rename
    assert files["pic.gif"].binary is True


def test_from_patch_tolerates_garbage() -> None:
    assert from_patch("").files == []
    assert from_patch("not a diff\n@@ -1 +1 @@\n").files == []
    truncated = "--- a/f\n+++ b/f\n@@ -1,5 +1,5 @@\n-x\n+y\n"
    assert by_path(from_patch(truncated))["f"].added == [(1, "y")]


@needs_git
def test_from_git_ignores_repo_env_vars(
    worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere" / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "bogus-index"))
    assert "keep.txt" in from_git(worktree, include_untracked=False).paths()


@needs_git
def test_from_git_unrelated_and_shallow_histories(tmp_path: Path, isolated_git: Path) -> None:
    repo = init_repo(tmp_path / "hist")
    stream = (
        fast_import_commit("main", "base", {"shared.txt": "base\n"})
        + fast_import_commit("feature", "mine", {"feature.txt": "mine\n"}, parent="refs/heads/main")
        + fast_import_commit("main", "upstream", {"upstream.txt": "theirs\n"})
        + fast_import_commit("orphan", "root", {"other.txt": "x\n"})
    )
    subprocess.run(["git", "fast-import", "--quiet"], cwd=repo, input=stream.encode(), check=True)
    git(repo, "checkout", "-q", "-f", "feature")

    # a full clone with unrelated histories: compare with the base commit itself
    unrelated = from_git(repo, base="orphan", include_untracked=False)
    assert sorted(unrelated.paths()) == ["feature.txt", "other.txt", "shared.txt"]

    # a shallow clone whose boundary hides the merge base must not blame upstream changes on us
    heads = subprocess.run(
        ["git", "rev-parse", "main", "feature"], cwd=repo, capture_output=True, check=True
    ).stdout.decode()
    (repo / ".git" / "shallow").write_text(heads, encoding="utf-8")
    with pytest.raises(GitError, match=r"clone is shallow.*fetch --deepen.*fetch-depth: 0"):
        from_git(repo, base="main")
