import json
import multiprocessing
import subprocess
from datetime import timedelta
from pathlib import Path

from tapin.store import MAX_TTL_HOURS, Store, iso, utcnow


def _handoff(store, session="s1", ttl=1):
    return store.create_handoff("claude", "# handoff", {"session_id": session}, ttl_hours=ttl)


def _rewrite_pending(store, **changes):
    store.pending_path.write_text(json.dumps(json.loads(store.pending_path.read_text()) | changes))


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


def test_unknown_keys_in_pending_are_ignored(tmp_path):
    store = Store(tmp_path)
    handoff_id = _handoff(store)
    _rewrite_pending(store, note="written by another tool")
    assert store.claim("codex", "c1").id == handoff_id


def test_expires_at_is_capped_at_max_ttl(tmp_path):
    store = Store(tmp_path)
    _handoff(store, ttl=24 * 365 * 50)
    _rewrite_pending(store, created_at=iso(utcnow() - timedelta(hours=MAX_TTL_HOURS + 1)))
    assert store.claimable() is None
    assert store.claim("codex", "c1") is None


def test_pending_created_in_the_future_is_not_claimable(tmp_path):
    store = Store(tmp_path)
    _handoff(store)
    _rewrite_pending(store, created_at=iso(utcnow() + timedelta(hours=1)))
    assert store.claim("codex", "c1") is None


def test_committed_tapin_is_never_claimed(repo):
    store = Store(repo)
    _handoff(store)
    subprocess.run(["git", "add", "-f", ".tapin"], cwd=repo, check=True, capture_output=True)
    assert store.claimable() is None
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
