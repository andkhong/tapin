import multiprocessing
from datetime import timedelta
from pathlib import Path

from tapin.store import Store


def _handoff(store, session="s1", ttl=1):
    return store.create_handoff("claude", "# handoff", {"session_id": session}, ttl_hours=ttl)


def test_create_and_claim_once(tmp_path):
    store = Store(tmp_path)
    handoff_id = _handoff(store)
    assert store.read_handoff(handoff_id) == "# handoff"
    assert store.claim("codex", "c1").id == handoff_id
    assert store.claim("cursor", "k1") is None


def test_originating_session_does_not_claim(tmp_path):
    store = Store(tmp_path)
    _handoff(store, session="s1")
    assert store.claim("claude", "s1") is None
    assert store.claim("claude", "s2") is not None


def test_expired_handoff_is_not_claimable(tmp_path):
    store = Store(tmp_path)
    _handoff(store, ttl=-1)
    assert store.claim("codex", "c1") is None


def test_claim_in_untouched_workspace_creates_nothing(tmp_path):
    assert Store(tmp_path).claim("codex", "c1") is None
    assert not (tmp_path / ".tapin").exists()


def _claim(root, n, results):
    results.put(Store(Path(root)).claim("codex", f"session-{n}") is not None)


def test_concurrent_claims_have_one_winner(tmp_path):
    _handoff(Store(tmp_path))
    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue()
    procs = [ctx.Process(target=_claim, args=(str(tmp_path), n, results)) for n in range(8)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()
    assert [results.get() for _ in procs].count(True) == 1


def test_begin_capture_dedupes_within_window(tmp_path):
    store = Store(tmp_path)
    assert store.begin_capture("cursor", "c1", timedelta(minutes=2))
    assert not store.begin_capture("cursor", "c1", timedelta(minutes=2))
    assert store.begin_capture("cursor", "c1", timedelta(0))


def test_latest_note(tmp_path):
    store = Store(tmp_path)
    store.append_note("claude", "decided on streaming parser")
    store.append_note("codex", "next: wire up retries")
    latest = store.latest_note()
    assert "next: wire up retries" in latest
    assert "streaming parser" not in latest
