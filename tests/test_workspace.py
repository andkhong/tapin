import subprocess

from tapin.workspace import ensure_excluded, snapshot


def test_snapshot_has_diff_and_new_files(repo):
    (repo / "app.py").write_text("def total(items):\n    return sum(i.price for i in items)\n")
    (repo / "ingest.py").write_text("def parse_row(row):\n    # TODO: finish\n")
    (repo / ".tapin").mkdir()
    (repo / ".tapin" / "pending.json").write_text("{}")

    snap = snapshot(repo, 60_000)

    assert snap.is_git and snap.branch == "main" and snap.head
    assert "i.price" in snap.diff
    assert snap.untracked == {"ingest.py": "def parse_row(row):\n    # TODO: finish\n"}
    assert ".tapin" not in snap.status
    assert not snap.truncated


def test_snapshot_truncates_to_budget(repo):
    (repo / "app.py").write_text("x = 1\n" * 5_000)
    snap = snapshot(repo, 200)
    assert snap.truncated
    assert len(snap.diff) == 200


def test_non_git_directory(tmp_path):
    assert snapshot(tmp_path, 100).is_git is False


def test_ensure_excluded_is_idempotent(repo):
    ensure_excluded(repo)
    ensure_excluded(repo)
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert exclude.splitlines().count(".tapin/") == 1
    (repo / ".tapin").mkdir()
    (repo / ".tapin" / "x").write_text("state")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert ".tapin" not in status
