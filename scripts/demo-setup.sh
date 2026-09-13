#!/bin/sh
# Builds a throwaway world for recording the Tap In demo (docs/DEMO.md): a small git repo with an uncommitted change,
# and a fake Claude Code session in it that ends on a usage limit.
#
#   eval "$(sh scripts/demo-setup.sh)"
#
# Everything is written under a new temporary directory; your real ~/.claude, ~/.codex and ~/.tapin are never touched.
# The output is shell code: `export` lines that point Tap In and the agents at the demo world, a `cd` into its repo,
# and the demo commands as comments. Delete the directory it names when you're done.
set -eu

DEMO=$(mktemp -d "${TMPDIR:-/tmp}/tapin-demo.XXXXXX")
DEMO=$(cd "$DEMO" && pwd -P)
case $DEMO in
  *[!A-Za-z0-9/._-]*)
    echo "demo-setup: $DEMO has characters this script doesn't quote; set TMPDIR to a plainer path" >&2
    exit 1
    ;;
esac

REPO=$DEMO/pipeline
CLAUDE_HOME=$DEMO/claude-home
SESSION=5f0c2d9e-7a41-4c3b-9d2e-1b6a8e4f7c10
SLUG=quoted-commas
mkdir -p "$REPO/tests" "$CLAUDE_HOME/plans" "$DEMO/codex-home" "$DEMO/tapin-home" "$DEMO/cursor-home"

# The project: a CSV importer, committed, then half-way through a change.
cd "$REPO"
git init -q
git symbolic-ref HEAD refs/heads/main
git config user.name "Tap In Demo"
git config user.email "demo@example.com"
git config commit.gpgsign false

cat >ingest.py <<'PY'
def parse_row(line):
    return line.rstrip("\n").split(",")


def load(path):
    with open(path) as f:
        return [parse_row(line) for line in f]
PY
cat >tests/test_ingest.py <<'PY'
from ingest import parse_row


def test_plain_row():
    assert parse_row("a,b,c") == ["a", "b", "c"]
PY
git add .
git commit -qm "CSV importer"

cat >ingest.py <<'PY'
import csv


def parse_row(line):
    return next(csv.reader([line.rstrip("\n")]))


def load(path):
    with open(path) as f:
        return [parse_row(line) for line in f]
PY
cat >>tests/test_ingest.py <<'PY'


def test_quoted_comma():
    assert parse_row('"Smith, Jane",42') == ["Smith, Jane", "42"]
PY

# The Claude Code session, in the log format Tap In's reader parses: a prompt, a plan, tool calls (one of them a failed
# command), a recap, and the API error record Claude Code writes when a usage limit stops it.
LOG_DIR=$CLAUDE_HOME/projects/$(printf '%s' "$REPO" | LC_ALL=C sed 's/[^A-Za-z0-9]/-/g')
LOG=$LOG_DIR/$SESSION.jsonl
mkdir -p "$LOG_DIR"
C="\"cwd\":\"$REPO\",\"sessionId\":\"$SESSION\""
cat >"$LOG" <<JSONL
{"type":"permission-mode","permissionMode":"default","sessionId":"$SESSION"}
{"type":"user",$C,"timestamp":"2026-09-13T17:02:11.000Z","message":{"role":"user","content":"parse_row splits \"Smith, Jane\" into two fields. Make it handle quoted commas and add a test."}}
{"type":"ai-title","aiTitle":"Handle quoted commas in the CSV importer","sessionId":"$SESSION"}
{"type":"assistant",$C,"slug":"$SLUG","message":{"id":"msg_01","role":"assistant","content":[{"type":"text","text":"Plan: 1. add a failing test for a quoted comma, 2. switch parse_row to csv.reader, 3. rerun the tests and check that load() still goes through parse_row."}]}}
{"type":"assistant",$C,"message":{"id":"msg_02","role":"assistant","content":[{"type":"tool_use","id":"toolu_01","name":"Read","input":{"file_path":"$REPO/ingest.py"}}]}}
{"type":"user",$C,"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_01","content":"def parse_row(line):\n    return line.rstrip(\"\\\\n\").split(\",\")"}]}}
{"type":"assistant",$C,"message":{"id":"msg_03","role":"assistant","content":[{"type":"tool_use","id":"toolu_02","name":"Edit","input":{"file_path":"$REPO/tests/test_ingest.py"}}]}}
{"type":"user",$C,"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_02","content":"The file tests/test_ingest.py has been updated."}]}}
{"type":"assistant",$C,"message":{"id":"msg_04","role":"assistant","content":[{"type":"tool_use","id":"toolu_03","name":"Bash","input":{"command":"python -m pytest -q"}}]}}
{"type":"user",$C,"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_03","is_error":true,"content":"Exit code 1\nFAILED tests/test_ingest.py::test_quoted_comma - AssertionError\n1 failed, 1 passed in 0.03s"}]}}
{"type":"assistant",$C,"message":{"id":"msg_05","role":"assistant","content":[{"type":"text","text":"The new test fails as expected: split(\",\") breaks the quoted field. Switching parse_row to csv.reader."}]}}
{"type":"assistant",$C,"message":{"id":"msg_06","role":"assistant","content":[{"type":"tool_use","id":"toolu_04","name":"Edit","input":{"file_path":"$REPO/ingest.py"}}]}}
{"type":"user",$C,"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_04","content":"The file ingest.py has been updated."}]}}
{"type":"assistant",$C,"message":{"id":"msg_07","role":"assistant","content":[{"type":"tool_use","id":"toolu_05","name":"TodoWrite","input":{"todos":[{"content":"Add a failing test for a quoted comma","status":"completed"},{"content":"Switch parse_row to csv.reader","status":"completed"},{"content":"Rerun the tests and check load()","status":"in_progress"}]}}]}}
{"type":"user",$C,"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_05","content":"Todos have been modified successfully."}]}}
{"type":"assistant",$C,"message":{"id":"msg_08","role":"assistant","content":[{"type":"text","text":"parse_row now uses csv.reader. Next I'll rerun the tests, then check load()."}]}}
{"type":"system","subtype":"away_summary",$C,"content":"Switched parse_row to csv.reader after the new quoted-comma test failed; the tests haven't been rerun yet. (disable recaps in /config)"}
{"type":"assistant",$C,"isApiErrorMessage":true,"error":"rate_limit","message":{"id":"msg_09","role":"assistant","content":[{"type":"text","text":"You've hit your usage limit. Resets at 7pm."}]}}
JSONL

cat >"$CLAUDE_HOME/plans/$SLUG.md" <<'MD'
# Handle quoted commas in the CSV importer

1. Add a failing test for a field with a quoted comma.
2. Switch `parse_row` to `csv.reader`.
3. Rerun the tests, then check that `load()` still goes through `parse_row`.
MD

# What Claude Code sends its StopFailure hook when a usage limit stops a turn.
cat >"$DEMO/stop-failure.json" <<JSON
{"hook_event_name":"StopFailure","session_id":"$SESSION","transcript_path":"$LOG","cwd":"$REPO","error":"rate_limit","last_assistant_message":"You've hit your usage limit. Resets at 7pm."}
JSON

cat <<EOF
# Tap In demo world in $DEMO (delete it when you're done)
export TAPIN_HOME='$DEMO/tapin-home'
export CLAUDE_CONFIG_DIR='$CLAUDE_HOME'
export CODEX_HOME='$DEMO/codex-home'
export TAPIN_CURSOR_DIR='$DEMO/cursor-home'
export TAPIN_DEMO='$DEMO'
cd '$REPO'
# Demo commands (docs/DEMO.md):
#   tapin hook claude stop-failure <"\$TAPIN_DEMO/stop-failure.json"   # Claude Code's StopFailure hook; captures in the background
#   tapin status
#   head -n 40 .tapin/handoffs/*/handoff.md
#   tapin to codex --print
EOF
