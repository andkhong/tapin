# Recording the demo

A 30 to 45 second GIF for the README: Claude Code stops on its usage limit, Tap In captures a handoff, and Codex is handed the work.

## Set up the demo world

From the checkout, with `tapin` on your PATH (`uv tool install --editable .`):

```sh
eval "$(sh scripts/demo-setup.sh)"
```

The script makes a temporary folder holding a small git repo (`pipeline`, a CSV importer with an uncommitted change) and a Claude Code session log in it that ends on a usage limit. Its output points `TAPIN_HOME`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `TAPIN_CURSOR_DIR` into that folder and changes into the repo, so your real `~/.claude`, `~/.codex` and `~/.tapin` aren't touched. Run `sh scripts/demo-setup.sh` without `eval` to see what it sets. Delete the folder when you're done.

Set `TAPIN_NO_NOTIFY=1` if you don't want a desktop notification to pop up while recording.

## Shot list

| # | Time | What it shows | Command |
|---|---|---|---|
| 1 | 0–10 s | Claude Code stops on its usage limit, and its `StopFailure` hook starts a capture in the background | `tapin hook claude stop-failure <"$TAPIN_DEMO/stop-failure.json"` |
| 2 | 10–17 s | The handoff waiting to be picked up | `tapin status` |
| 3 | 17–30 s | What the next agent reads: the stop, how to continue, the plan | `head -n 40 .tapin/handoffs/*/handoff.md` |
| 4 | 30–40 s | Handing the work to Codex, with the prompt it gets | `tapin to codex --print` |

Shot 1 pipes in the same payload Claude Code gives its `StopFailure` hook. To write the handoff directly instead, run `tapin capture --from claude --reason rate_limit`.

Pause about two seconds after shot 1 so the background capture finishes before `tapin status`. `tapin to codex --print` copies the prompt to the clipboard when it can (`pbcopy`, `wl-copy`, `xclip` or `xsel`); the `Launch:` line shows it either way.

## Record with VHS

[VHS](https://github.com/charmbracelet/vhs) replays [`demo.tape`](demo.tape), which runs the setup off camera and then types the four commands:

```sh
brew install vhs
vhs docs/demo.tape   # writes docs/images/demo.gif
```

## Record with asciinema and agg

```sh
brew install asciinema agg
eval "$(sh scripts/demo-setup.sh)"
asciinema rec demo.cast   # run the four commands, then exit
agg demo.cast docs/images/demo.gif
```

## Add it to the README

Put the GIF under the tagline, using the same raw URL form as the other images so it also shows on PyPI:

```markdown
![Claude Code hits its usage limit, Tap In captures a handoff, and Codex picks it up](https://raw.githubusercontent.com/andkhong/tapin/main/docs/images/demo.gif)
```
