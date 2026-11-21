from __future__ import annotations

import itertools
import json
import random
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from ruleproof.errors import TranscriptError
from ruleproof.models import Session
from ruleproof.transcripts import cache, claude

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"
MAIN = FIXTURES / "claude" / "11111111-2222-3333-4444-555555555555.jsonl"


def records(n: int, start: int = 0) -> list[dict[str, object]]:
    """Synthetic Claude Code records: prompts, prose re-emitted by a streaming writer, shell
    calls whose results arrive in a later record, and Write calls."""
    out: list[dict[str, object]] = []
    for i in range(start, start + n):
        ts = f"2026-11-21T00:{i // 60 % 60:02d}:{i % 60:02d}Z"
        base = {"sessionId": "s1", "cwd": "/repo", "timestamp": ts}
        tid = f"toolu_{i}"
        tool = (
            {"name": "Bash", "input": {"command": f"uv run pytest -k t{i}"}}
            if i % 2
            else {"name": "Write", "input": {"file_path": f"/repo/f{i}.py", "content": "x"}}
        )
        msg = {"id": f"msg_{i}", "model": "claude-synthetic"}
        text = {"type": "text", "text": f"step {i} ünïcode"}
        out += [
            {**base, "type": "user", "message": {"content": f"please do step {i}"}},
            {**base, "type": "assistant", "message": {**msg, "content": [text]}},
            {**base, "type": "assistant", "message": {**msg, "content": [text]}},  # re-emitted
            {
                **base,
                "type": "assistant",
                "message": {**msg, "content": [{"type": "tool_use", "id": tid, **tool}]},
            },
            {
                **base,
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tid,
                            "content": "Exit code 1" if i % 3 == 0 else "ok",
                            "is_error": i % 3 == 0,
                        }
                    ]
                },
                "toolUseResult": {"type": "create"} if i % 2 == 0 else {"stdout": "ok"},
            },
        ]
    return out


def jsonl(recs: list[dict[str, object]], newline: str = "\n") -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False) + newline for r in recs).encode()


def assert_same(path: Path, cache_dir: Path | None) -> Session:
    """The incremental read equals a full read of the file as it is now."""
    got = cache.load(path, cache_dir)
    assert got == claude.parse(path)
    return got


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


def test_appends_are_read_incrementally_and_match_a_full_read(
    tmp_path: Path, cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "s.jsonl"
    path.write_bytes(jsonl(records(20)))
    first = assert_same(path, cache_dir)
    with open(path, "ab") as fh:
        fh.write(jsonl(records(5, start=20)))
    fed: list[int] = []
    real_feed = claude.feed

    def counting_feed(*args: object, **kw: object) -> None:
        fed.append(1)
        real_feed(*args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(claude, "feed", counting_feed)
    second = cache.load(path, cache_dir)
    assert len(fed) == 25  # only the appended records were parsed
    monkeypatch.undo()
    assert second == claude.parse(path)
    assert len(second.events) > len(first.events)
    assert [e.exit_code for e in second.commands()][-1] is not None  # results joined their calls


def test_subagent_transcripts_are_cached_too(tmp_path: Path, cache_dir: Path) -> None:
    root = tmp_path / "copy"
    shutil.copytree(MAIN.parent, root)
    path = root / MAIN.name
    assert_same(path, cache_dir)
    sub = next((path.with_suffix("") / "subagents").rglob("agent-*.jsonl"))
    with open(sub, "ab") as fh:
        fh.write(jsonl(records(2, start=100)))
    assert_same(path, cache_dir)


@pytest.mark.parametrize("change", ["truncate", "rewrite_head", "rewrite_tail", "replace"])
def test_a_shrunk_or_rewritten_transcript_is_read_again(
    tmp_path: Path, cache_dir: Path, change: str
) -> None:
    path = tmp_path / "s.jsonl"
    data = jsonl(records(30))
    path.write_bytes(data)
    assert_same(path, cache_dir)
    if change == "truncate":
        path.write_bytes(data[: data.index(b"\n", len(data) // 2) + 1])
    elif change == "rewrite_head":  # same length, different start (compaction-like rewrite)
        path.write_bytes(data.replace(b"please do step 0", b"PLEASE DO STEP 0", 1))
    elif change == "rewrite_tail":  # same length, last record changed
        cut = data.rindex(b'"content": "ok"') + len(b'"content": "')
        path.write_bytes(data[:cut] + b"OK" + data[cut + 2 :])
    else:  # a different, longer session at the same path
        path.write_bytes(jsonl(records(40, start=500)))
    assert_same(path, cache_dir)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_a_torn_last_line_is_read_again_once_complete(
    tmp_path: Path, cache_dir: Path, newline: str
) -> None:
    path = tmp_path / "s.jsonl"
    whole = jsonl(records(6), newline)
    cut = len(whole) - 40
    path.write_bytes(whole[:cut])  # the writer is half way through the last record
    torn = assert_same(path, cache_dir)
    assert any("invalid JSON" in w for w in torn.warnings)
    path.write_bytes(whole)
    done = assert_same(path, cache_dir)
    assert not done.warnings


def test_random_append_splits_match_a_full_read(tmp_path: Path, cache_dir: Path) -> None:
    # Property-style: any sequence of appends, cut at any byte, reads like the whole file.
    data = MAIN.read_bytes() + jsonl(records(12, start=200), "\r\n") + b"not json\n\n"
    data += '{"type": "user", "message": {"content": "tail ☃"}}\n'.encode()
    rng = random.Random(20261121)
    path = tmp_path / "s.jsonl"
    for _ in range(25):
        shutil.rmtree(cache_dir, ignore_errors=True)
        cuts = sorted(rng.sample(range(1, len(data)), rng.randint(1, 8)))
        path.write_bytes(b"")
        for a, b in itertools.pairwise([0, *cuts, len(data)]):
            with open(path, "ab") as fh:
                fh.write(data[a:b])
            try:
                full = claude.parse(path)
            except TranscriptError:
                with pytest.raises(TranscriptError):
                    cache.load(path, cache_dir)
                continue
            assert cache.load(path, cache_dir) == full


def test_a_bad_cache_falls_back_to_a_full_read(tmp_path: Path, cache_dir: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_bytes(jsonl(records(10)))
    assert_same(path, cache_dir)
    store = cache.cache_file(path, cache_dir)
    good = json.loads(store.read_bytes())
    stream = good["streams"][str(path)]
    broken: list[object] = [
        b"{not json",
        {**good, "format": cache.FORMAT + 1},
        {**good, "ruleproof": "0.0.0"},
        {**good, "path": str(tmp_path / "other.jsonl")},
        {**good, "streams": {str(path): {**stream, "events": [["no-such-kind"]]}}},
        {**good, "streams": {str(path): {**stream, "offset": -5}}},
        {**good, "streams": {str(path): {**stream, "pending": {"t": [10**6, False]}}}},
        [1, 2, 3],
    ]
    for bad in broken:
        store.write_bytes(bad if isinstance(bad, bytes) else json.dumps(bad).encode())
        assert_same(path, cache_dir)


def test_an_unwritable_cache_still_gives_the_full_result(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_bytes(jsonl(records(5)))
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x", encoding="utf-8")
    assert_same(path, blocker / "cache")
    assert_same(path, None)


def test_a_json_array_transcript_is_read_whole(tmp_path: Path, cache_dir: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps(records(3), indent=1), encoding="utf-8")
    assert_same(path, cache_dir)
    assert not cache_dir.exists()  # nothing to resume from in a single JSON document


def test_no_claude_records_is_an_error(tmp_path: Path, cache_dir: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text('{"foo": 1}\n', encoding="utf-8")
    with pytest.raises(TranscriptError, match="no Claude Code records"):
        cache.load(path, cache_dir)
    with pytest.raises(TranscriptError, match="not found"):
        cache.load(tmp_path / "missing.jsonl", cache_dir)


def test_a_read_past_its_deadline_saves_progress_and_resumes(
    tmp_path: Path, cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No wall clock: the clock is a counter, so the deadline passes after the first chunk.
    monkeypatch.setattr(cache, "_CHUNK", 512)
    path = tmp_path / "s.jsonl"
    path.write_bytes(jsonl(records(20)))
    size = path.stat().st_size

    def clock() -> Callable[[], float]:  # a fresh counter per call: 0 at the start
        ticks = itertools.count()
        return lambda: float(next(ticks))

    done: list[int] = []
    for _ in range(path.read_bytes().count(b"\n") + 1):  # at least a line per call
        try:
            got = cache.load(path, cache_dir, deadline=0.0, clock=clock())
        except cache.BudgetExceeded as exc:
            assert exc.saved and exc.total == size
            assert not done or exc.done > done[-1]  # every call makes progress
            done.append(exc.done)
            continue
        break
    else:
        pytest.fail("the read never finished")
    assert len(done) > 3
    assert got == claude.parse(path)
    assert cache.load(path, cache_dir, deadline=0.0, clock=clock()) == got  # nothing left to read


def test_loading_the_cache_counts_against_the_deadline(tmp_path: Path, cache_dir: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_bytes(jsonl(records(5)))
    cache.load(path, cache_dir)
    with pytest.raises(cache.BudgetExceeded) as exc:
        cache.load(path, cache_dir, deadline=0.0, clock=lambda: 1.0)
    assert not exc.value.saved  # a retry would load the same cache again


def test_pruning_deletes_only_old_caches_ruleproof_wrote(tmp_path: Path, cache_dir: Path) -> None:
    import os

    cache_dir.mkdir()
    old = 1_000_000_000  # 2001
    mine = cache_dir / ("ab" * 16 + ".json")
    mine_tmp = cache_dir / ("ab" * 16 + "x1y2z3_q.tmp")
    others = [cache_dir / n for n in ("other.json", "x.tmp", "settings.json", "ab.json")]
    for f in [mine, mine_tmp, *others]:
        f.write_text("{}", encoding="utf-8")
        os.utime(f, (old, old))
    path = tmp_path / "s.jsonl"
    path.write_bytes(jsonl(records(2)))
    assert_same(path, cache_dir)  # a fresh write prunes
    assert not mine.exists() and not mine_tmp.exists()
    assert all(f.exists() for f in others)


def test_a_read_that_finishes_on_its_last_chunk_is_in_time(tmp_path: Path, cache_dir: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_bytes(jsonl(records(3)))
    assert cache.load(path, cache_dir, deadline=0.0, clock=lambda: 1.0) == claude.parse(path)


def test_concurrent_readers_do_not_corrupt_the_cache(tmp_path: Path, cache_dir: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_bytes(jsonl(records(40)))
    script = (
        "import sys\nfrom pathlib import Path\nfrom ruleproof.transcripts import cache\n"
        "for _ in range(15):\n    cache.load(Path(sys.argv[1]), Path(sys.argv[2]))\n"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(path), str(cache_dir)], stderr=subprocess.PIPE
        )
        for _ in range(2)
    ]
    for i in range(10):  # the session keeps growing while both read it
        with open(path, "ab") as fh:
            fh.write(jsonl(records(4, start=100 + 4 * i)))
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()
    assert json.loads(cache.cache_file(path, cache_dir).read_bytes())["format"] == cache.FORMAT
    assert not list(cache_dir.glob("*.tmp"))
    assert_same(path, cache_dir)


def test_default_cache_dir_is_outside_the_repo(tmp_path: Path) -> None:
    # A folder of its own even when the variable names a shared one: old caches get deleted.
    assert cache.default_cache_dir({cache.CACHE_DIR_ENV: str(tmp_path)}) == tmp_path / "transcripts"
    default = cache.default_cache_dir({})
    assert default.parts[-2:] == ("ruleproof", "transcripts")
    assert Path.home() in default.parents
