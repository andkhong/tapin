import json

from tapin.readers.continues import ContinuesReader


def test_continues_rebuilds_index_and_picks_newest_session_in_workspace(tmp_path, monkeypatch):
    items = [
        {"id": "older", "cwd": str(tmp_path), "updatedAt": "2026-09-12T20:00:00.000Z", "originalPath": "/logs/older.jsonl"},
        {"id": "newer", "cwd": str(tmp_path / "sub"), "updatedAt": "2026-09-12T21:00:00.000Z", "originalPath": "/logs/newer.jsonl"},
        {"id": "elsewhere", "cwd": "/somewhere/else", "updatedAt": "2026-09-12T22:00:00.000Z"},
    ]
    calls = []
    reader = ContinuesReader(["continues"], timeout=10)
    monkeypatch.setattr(reader, "_run", lambda *args: calls.append(args) or json.dumps(items))

    ref = reader.find_session("claude", tmp_path)

    assert ref.session_id == "newer"
    assert ref.path == "/logs/newer.jsonl"
    assert ref.updated_at == "2026-09-12T21:00:00Z"
    assert "--rebuild" in calls[0]
    assert reader.find_session("claude", tmp_path, session_id="elsewhere").session_id == "elsewhere"
