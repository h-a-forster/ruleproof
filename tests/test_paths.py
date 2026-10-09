from __future__ import annotations

import pytest

from ruleproof.paths import glob_match, in_scope, match_any, normalize, relpath_in_repo


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("x.bak", "*.bak", True),
        ("a/b/x.bak", "*.bak", True),
        ("a/b/x.bak.txt", "*.bak", False),
        ("src/a.py", "src/*.py", True),
        ("src/b/a.py", "src/*.py", False),
        ("src/b/a.py", "src/**/*.py", True),
        ("src/a.py", "src/**/*.py", True),
        ("src/a.py", "src/**", True),
        ("srcx/a.py", "src/**", False),
        ("vendor/x/y.js", "vendor/", True),
        ("a/vendor/x.js", "vendor/", False),
        ("foo.before_cleanup", "*.before_*", True),
        ("CHANGELOG.md", "CHANGELOG.md", True),
        ("docs/CHANGELOG.md", "CHANGELOG.md", True),
        ("docs/CHANGELOG.md", "/CHANGELOG.md", False),
        ("CHANGELOG.md", "/CHANGELOG.md", True),
        ("a.py", "[ab].py", True),
        ("c.py", "[ab].py", False),
        ("c.py", "[!ab].py", True),
        ("tests/test_x.py", "test_*.py", True),
        ("a/b/c/d.txt", "a/**/d.txt", True),
        ("a/d.txt", "a/**/d.txt", True),
        ("a/x**y/z", "a/x**y/z", True),
        ("Src/a.py", "src/*.py", False),
        ("./src/a.py", "src/*.py", True),
        ("src\\a.py", "src/*.py", True),
    ],
)
def test_glob_match(path: str, pattern: str, expected: bool) -> None:
    assert glob_match(path, pattern) is expected


def test_match_any() -> None:
    assert match_any("a/x.orig", ["*.bak", "*.orig"])
    assert not match_any("a/x.py", ["*.bak", "*.orig"])
    assert not match_any("a/x.py", [])


def test_normalize() -> None:
    assert normalize("./a\\b/c") == "a/b/c"
    assert normalize("/a/b") == "a/b"


@pytest.mark.parametrize(
    ("path", "scope", "expected"),
    [
        ("pkg/a.py", "pkg", True),
        ("pkg/a.py", "pkg/", True),
        ("pkgx/a.py", "pkg", False),
        ("pkg", "pkg", True),
        ("a.py", None, True),
        ("a.py", "", True),
    ],
)
def test_in_scope(path: str, scope: str | None, expected: bool) -> None:
    assert in_scope(path, scope) is expected


@pytest.mark.parametrize(
    ("path", "repo", "cwd", "expected"),
    [
        (r"C:\Users\x\proj\src\a.py", r"C:\Users\x\proj", None, "src/a.py"),
        ("c:/users/x/PROJ/src/a.py", r"C:\Users\x\proj", None, "src/a.py"),
        ("/c/Users/x/proj/b.py", "C:/Users/x/proj", None, "b.py"),
        ("src/../c.py", "C:/Users/x/proj", None, "c.py"),
        ("a.py", "C:/Users/x/proj", "C:/Users/x/proj/pkg", "pkg/a.py"),
        ("C:/Users/x/other/a.py", "C:/Users/x/proj", None, None),
        ("../other/a.py", "C:/Users/x/proj", None, None),
        ("C:/Users/x/proj", "C:/Users/x/proj", None, ""),
        ("C:/Users/x/project2/a.py", "C:/Users/x/proj", None, None),
        ("/home/u/proj/src/a.py", "/home/u/proj", None, "src/a.py"),
        ("/home/u/proj2/a.py", "/home/u/proj", None, None),
        ("a.py", "/home/u/proj", "/home/u/proj/sub", "sub/a.py"),
        ("../../etc/passwd", "/home/u/proj", None, None),
    ],
)
def test_relpath_in_repo(path: str, repo: str, cwd: str | None, expected: str | None) -> None:
    assert relpath_in_repo(path, repo, cwd) == expected


def test_relpath_without_repo() -> None:
    assert relpath_in_repo("src/a.py", None) == "src/a.py"
    assert relpath_in_repo("/abs/a.py", None) is None
