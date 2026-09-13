import subprocess
from pathlib import Path

from tapin.workspace import ensure_excluded, find_root, snapshot


def test_find_root_matches_git_toplevel(repo, tmp_path):
    def toplevel(path):
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True, check=True)
        return Path(result.stdout.strip())

    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    worktree = tmp_path / "linked"
    subprocess.run(["git", "worktree", "add", "-q", str(worktree)], cwd=repo, check=True, capture_output=True)
    (worktree / "docs").mkdir()

    for path in (repo, nested, worktree, worktree / "docs"):
        assert find_root(path) == toplevel(path)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert find_root(plain) == plain.resolve()


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


def test_secret_looking_untracked_files_are_listed_without_contents(repo):
    (repo / ".env").write_text("OPENAI_API_KEY=super-secret-value\n")
    (repo / "server.key").write_text("-----BEGIN PRIVATE KEY-----\nsecret-value\n")
    (repo / "notes.txt").write_text("nothing sensitive\n")

    snap = snapshot(repo, 60_000)

    assert snap.untracked[".env"] == "(skipped: looks like a secrets file)"
    assert snap.untracked["server.key"] == "(skipped: looks like a secrets file)"
    assert "super-secret-value" not in str(snap.untracked)
    assert snap.untracked["notes.txt"] == "nothing sensitive\n"


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
