#!/bin/sh
# End-to-end test of install.sh, run in CI and locally: sh tests/installer/test_install_sh.sh
#
# Builds a wheel from this checkout and installs it with install.sh into a throwaway HOME, with fake `claude` and
# `codex` CLIs first on a minimal PATH. Checks the launcher, hooks, doctor, a second run, the PyPI fallback and
# --uninstall. Needs uv. Set KEEP_WORK=1 to keep the scratch directory.
set -eu

REPO=$(cd "$(dirname "$0")/../.." && pwd)
REAL_UV=$(command -v uv) || {
  echo "uv is required" >&2
  exit 1
}
WORK=$(mktemp -d "${TMPDIR:-/tmp}/tapin-installer-test.XXXXXX")
WORK=$(cd "$WORK" && pwd -P)
trap '[ "${KEEP_WORK:-}" = 1 ] || rm -rf "$WORK"' EXIT

step() {
  printf '\n== %s\n' "$*"
}

check() {
  description=$1
  shift
  if "$@"; then
    printf 'ok   %s\n' "$description"
  else
    printf 'FAIL %s\n' "$description"
    exit 1
  fi
}

has_once() {
  [ "$(grep -c -F -- "$1" "$2" 2>/dev/null)" = 1 ]
}

absent() {
  ! grep -q -F -- "$1" "$2" 2>/dev/null
}

run_logged() {
  log=$1
  shift
  if ! "$@" >"$log" 2>&1; then
    cat "$log"
    printf 'FAIL %s exited non-zero\n' "$*"
    exit 1
  fi
  cat "$log"
}

backups() {
  find "$HOME" -name '*.tapin-backup-*' | wc -l | tr -d ' '
}

PYTHON=$(cd "$WORK" && "$REAL_UV" python find --system ">=3.11" 2>/dev/null || true)

unset XDG_CONFIG_HOME XDG_DATA_HOME XDG_CACHE_HOME XDG_BIN_HOME XDG_STATE_HOME UV_INSTALL_DIR UV_PYTHON VIRTUAL_ENV TAPIN_SPEC
export HOME="$WORK/home"
export TAPIN_HOME="$HOME/.tapin" CLAUDE_CONFIG_DIR="$HOME/.claude" CODEX_HOME="$HOME/.codex" TAPIN_CURSOR_DIR="$HOME/.cursor"
export UV_TOOL_DIR="$WORK/uv/tools" UV_TOOL_BIN_DIR="$WORK/uv/bin" UV_CACHE_DIR="$WORK/uv/cache" UV_PYTHON_INSTALL_DIR="$WORK/uv/python"
export UV_NO_MODIFY_PATH=1 TAPIN_NO_NOTIFY=1
mkdir -p "$HOME" "$WORK/bin"

for name in claude codex; do
  cat >"$WORK/bin/$name" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >>"$WORK/$name-calls.log"
EOF
  chmod +x "$WORK/bin/$name"
done
ln -s "$REAL_UV" "$WORK/bin/uv"
if [ -n "$PYTHON" ]; then
  ln -s "$PYTHON" "$WORK/bin/python3"
fi
export PATH="$WORK/bin:/usr/bin:/bin"

LAUNCHER="$TAPIN_HOME/bin/tapin"
TOOL="$UV_TOOL_BIN_DIR/tapin"
CLAUDE_SETTINGS="$CLAUDE_CONFIG_DIR/settings.json"
CODEX_HOOKS="$CODEX_HOME/hooks.json"

launcher_runs_the_tool() {
  [ "$(readlink "$LAUNCHER")" = "$UV_TOOL_DIR/tapin/bin/tapin" ] && "$LAUNCHER" --help >/dev/null
}

launcher_gone() {
  [ ! -e "$LAUNCHER" ] && [ ! -L "$LAUNCHER" ]
}

installed_default_spec() {
  [ "$(grep '^tool install ' "$WORK/fake-uv/calls.log" | awk '{print $NF}')" = "$DEFAULT_SPEC" ]
}

installed_with_reinstall() {
  grep '^tool install ' "$WORK/fake-uv/calls.log" | grep -q -- ' --reinstall '
}

tool_not_listed() {
  ! "$REAL_UV" tool list 2>/dev/null | grep -q '^tapin '
}

hooks_installed_once() {
  check "Claude Code SessionStart hook calls the launcher, once" has_once "\"$LAUNCHER hook claude session-start\"" "$CLAUDE_SETTINGS"
  check "Claude Code StopFailure hook calls the launcher, once" has_once "\"$LAUNCHER hook claude stop-failure\"" "$CLAUDE_SETTINGS"
  check "Codex SessionStart hook calls the launcher, once" has_once "\"$LAUNCHER hook codex session-start\"" "$CODEX_HOOKS"
  check "Codex Stop hook calls the launcher, once" has_once "\"$LAUNCHER hook codex stop\"" "$CODEX_HOOKS"
  check "Claude Code PostToolUse hook calls the launcher, once" has_once "\"$LAUNCHER hook claude post-tool-use\"" "$CLAUDE_SETTINGS"
  check "Codex PostToolUse hook calls the launcher, once" has_once "\"$LAUNCHER hook codex post-tool-use\"" "$CODEX_HOOKS"
  check "Claude Code status line calls the launcher, once" has_once "\"$LAUNCHER statusline\"" "$CLAUDE_SETTINGS"
}

step "build a wheel from $REPO"
run_logged "$WORK/build.log" "$REAL_UV" build --wheel --out-dir "$WORK/dist" "$REPO"
for wheel in "$WORK"/dist/tapin-*.whl; do
  WHEEL=$wheel
done

step "install.sh with TAPIN_SPEC set to the wheel"
run_logged "$WORK/install-1.log" env TAPIN_SPEC="$WHEEL" sh "$REPO/install.sh"
check "tapin tool installed in UV_TOOL_BIN_DIR" test -x "$TOOL"
check "launcher is a symlink to the uv tool's tapin and runs" launcher_runs_the_tool
hooks_installed_once
check "Claude Code MCP server registered with the launcher" grep -q -F -- "mcp add --scope user tapin -- $LAUNCHER mcp" "$WORK/claude-calls.log"
check "Codex MCP server registered with the launcher" grep -q -F -- "mcp add tapin -- $LAUNCHER mcp" "$WORK/codex-calls.log"
check "Cursor skipped because it isn't installed" grep -q "Cursor: skipped, not detected" "$WORK/install-1.log"
check "Codex trust step printed" grep -q -F "    /hooks" "$WORK/install-1.log"
check "PATH hint printed" grep -q "isn't on your PATH" "$WORK/install-1.log"

step "tapin doctor"
doctor_status=0
"$TOOL" doctor >"$WORK/doctor.log" 2>&1 || doctor_status=$?
cat "$WORK/doctor.log"
check "doctor reports no failures" test "$doctor_status" -eq 0
check "doctor: launcher resolves" grep -q -F "ok   launcher $LAUNCHER runs" "$WORK/doctor.log"
check "doctor: Claude Code hooks call the launcher" grep -q -F "ok   Claude Code hooks in $CLAUDE_SETTINGS call the launcher" "$WORK/doctor.log"
check "doctor: Codex hooks call the launcher" grep -q -F "ok   Codex hooks in $CODEX_HOOKS call the launcher" "$WORK/doctor.log"
check "doctor: Codex trust not recorded yet" grep -q -F "warn Codex SessionStart hook: not trusted yet" "$WORK/doctor.log"
check "doctor: Codex PostToolUse trust not recorded yet" grep -q -F "warn Codex PostToolUse hook: not trusted yet" "$WORK/doctor.log"
check "doctor: Claude Code status line is Tap In's" grep -q -F "ok   Claude Code status line in $CLAUDE_SETTINGS is Tap In's" "$WORK/doctor.log"

step "install.sh again"
backups_before=$(backups)
run_logged "$WORK/install-2.log" env TAPIN_SPEC="$WHEEL" sh "$REPO/install.sh"
hooks_installed_once
check "launcher unchanged" grep -q "launcher unchanged" "$WORK/install-2.log"
check "hook files unchanged" test "$(grep -c "hooks already installed" "$WORK/install-2.log")" -eq 2
check "no new config backups" test "$(backups)" -eq "$backups_before"

step "DEFAULT_SPEC when TAPIN_SPEC is unset, with a fake uv"
DEFAULT_SPEC=$(sed -n 's/^DEFAULT_SPEC="\(.*\)"$/\1/p' "$REPO/install.sh")
check "install.sh sets DEFAULT_SPEC" test -n "$DEFAULT_SPEC"
mkdir -p "$WORK/fake-uv/bin" "$WORK/fake-uv/tools"
cat >"$WORK/fake-uv/bin/uv" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >>"$WORK/fake-uv/calls.log"
case "\$*" in
  --version) echo "uv 0.0.0 (fake)" ;;
  "tool dir --bin") echo "$WORK/fake-uv/tools" ;;
  "tool install "*)
    printf '#!/bin/sh\n' >"$WORK/fake-uv/tools/tapin"
    chmod +x "$WORK/fake-uv/tools/tapin"
    ;;
  *) exit 2 ;;
esac
EOF
chmod +x "$WORK/fake-uv/bin/uv"
run_logged "$WORK/default-spec.log" env PATH="$WORK/fake-uv/bin:$PATH" TAPIN_SKIP_AGENT_SETUP=1 sh "$REPO/install.sh"
check "uv tool install was given DEFAULT_SPEC" installed_default_spec
check "uv tool install was given --reinstall, so a re-run updates a same-version build" installed_with_reinstall
check "agent setup skipped" grep -q "TAPIN_SKIP_AGENT_SETUP=1" "$WORK/default-spec.log"

step "install.sh --uninstall"
run_logged "$WORK/uninstall.log" sh "$REPO/install.sh" --uninstall
check "Claude Code hooks removed" absent "hook claude" "$CLAUDE_SETTINGS"
check "Claude Code status line removed" absent "statusline" "$CLAUDE_SETTINGS"
check "Codex hooks removed" absent "hook codex" "$CODEX_HOOKS"
check "Claude Code MCP server removed" test "$(tail -n 1 "$WORK/claude-calls.log")" = "mcp remove --scope user tapin"
check "Codex MCP server removed" test "$(tail -n 1 "$WORK/codex-calls.log")" = "mcp remove tapin"
check "launcher removed" launcher_gone
check "no Cursor config created for a Cursor that was never set up" test ! -e "$TAPIN_CURSOR_DIR"
check "tapin tool removed" test ! -e "$TOOL"
check "uv no longer lists tapin" tool_not_listed

printf '\nAll installer checks passed.\n'
