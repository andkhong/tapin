#!/bin/sh
# Tap In installer: https://github.com/andkhong/tapin
#
#   curl -LsSf https://raw.githubusercontent.com/andkhong/tapin/main/install.sh | sh
#   curl -LsSf https://raw.githubusercontent.com/andkhong/tapin/main/install.sh | sh -s -- --uninstall
#
# Installs uv if it's missing (uv downloads Python 3.11+ when needed), installs Tap In with `uv tool install`,
# then runs `tapin install` to add hooks and the MCP server to the agents found on this machine.
#
# Environment variables:
#   TAPIN_SPEC=<spec>          install this instead of DEFAULT_SPEC: a package name, wheel path or URL
#   TAPIN_INSTALL_ARGS=<args>  extra arguments for `tapin install`, for example "--agents claude,codex"
#   TAPIN_SKIP_AGENT_SETUP=1   install the tool only and leave agent configs alone
#   TAPIN_NO_UV_INSTALL=1      stop instead of installing uv
set -eu

# Becomes "tapin" after the first PyPI release (docs/RELEASING.md).
DEFAULT_SPEC="https://github.com/andkhong/tapin/archive/refs/heads/main.tar.gz"
PYTHON_REQUEST=">=3.11"

say() {
  printf 'tapin: %s\n' "$*" >&2
}

die() {
  say "$*"
  exit 1
}

usage() {
  cat <<'EOF'
Usage: install.sh [--uninstall]

Installs Tap In with uv and sets up the coding agents found on this machine.

  --uninstall   remove Tap In's hooks, MCP registrations and launcher, then the tapin tool

Environment: TAPIN_SPEC, TAPIN_INSTALL_ARGS, TAPIN_SKIP_AGENT_SETUP=1, TAPIN_NO_UV_INSTALL=1
EOF
}

find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  for candidate in "${UV_INSTALL_DIR:-}/uv" "${XDG_BIN_HOME:-}/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

ensure_uv() {
  if UV=$(find_uv); then
    return 0
  fi
  if [ "${TAPIN_NO_UV_INSTALL:-}" = 1 ]; then
    die "uv not found. Install it from https://docs.astral.sh/uv/ and run this again."
  fi
  say "uv not found, installing it with Astral's installer from https://astral.sh/uv"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh >&2
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh >&2
  else
    die "curl or wget is needed to install uv"
  fi
  UV=$(find_uv) || die "uv was installed but can't be found. Open a new shell and run this again."
}

tool_install() {
  "$UV" tool install --upgrade --reinstall --python "$PYTHON_REQUEST" "$1" </dev/null
}

install_tapin() {
  spec=${TAPIN_SPEC:-$DEFAULT_SPEC}
  say "installing $spec"
  tool_install "$spec" >&2
}

run_install() {
  ensure_uv
  say "using $("$UV" --version) from $UV"
  install_tapin

  bin_dir=$("$UV" tool dir --bin)
  tapin="$bin_dir/tapin"
  [ -x "$tapin" ] || die "uv installed tapin, but $tapin is missing"

  if [ "${TAPIN_SKIP_AGENT_SETUP:-}" = 1 ]; then
    say "skipped agent setup because TAPIN_SKIP_AGENT_SETUP=1. Run \`tapin install\` when you're ready."
  else
    say "setting up the agents found on this machine"
    set -f
    # shellcheck disable=SC2086 # TAPIN_INSTALL_ARGS is a list of arguments
    "$tapin" install ${TAPIN_INSTALL_ARGS:-} </dev/null
    set +f
  fi

  case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) say "$bin_dir isn't on your PATH. To run \`tapin\` yourself, add it, or run \`$UV tool update-shell\` and open a new shell." ;;
  esac
  say "done. Run \`tapin doctor\` to check the setup."
}

run_uninstall() {
  UV=$(find_uv) || die "uv not found, so there is no uv-installed Tap In to remove"
  bin_dir=$("$UV" tool dir --bin)
  if [ -x "$bin_dir/tapin" ]; then
    say "removing Tap In from agent configs"
    "$bin_dir/tapin" uninstall </dev/null
  else
    say "no tapin in $bin_dir, so agent configs were left alone"
  fi
  if "$UV" tool list 2>/dev/null | grep -q '^tapin '; then
    "$UV" tool uninstall tapin >&2
  fi
  say "done. Handoffs in your projects' .tapin/ folders and ${TAPIN_HOME:-$HOME/.tapin} (config and hook log) were kept."
}

main() {
  action=run_install
  for arg in "$@"; do
    case "$arg" in
      --uninstall) action=run_uninstall ;;
      -h | --help)
        usage
        return 0
        ;;
      *) die "unknown option: $arg (see --help)" ;;
    esac
  done
  "$action"
}

main "$@"
