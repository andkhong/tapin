# The README demo, following the shot list in docs/DEMO.md. Run it from the checkout: vhs docs/demo.tape
# Needs `tapin` on PATH (uv tool install --editable .). The setup runs off camera in a throwaway demo world.

Output docs/images/demo.gif

Require tapin
Require git

Set Shell "bash"
Set FontSize 18
Set Width 1280
Set Height 760
Set Padding 24
Set TypingSpeed 35ms

Hide
Type `export TAPIN_NO_NOTIFY=1 && eval "$(sh scripts/demo-setup.sh)" && clear`
Enter
Sleep 2s
Show

# 1. Claude Code stops on its usage limit; its StopFailure hook captures in the background.
Type "# Claude Code just hit its usage limit. Its StopFailure hook runs:"
Enter
Sleep 800ms
Type `tapin hook claude stop-failure <"$TAPIN_DEMO/stop-failure.json"`
Sleep 500ms
Enter
Sleep 3s

# 2. The handoff waiting to be picked up.
Type "tapin status"
Sleep 500ms
Enter
Sleep 5s

# 3. What the next agent reads.
Type "head -n 40 .tapin/handoffs/*/handoff.md"
Sleep 500ms
Enter
Sleep 10s

# 4. Hand the work to Codex, with the prompt it gets.
Type "tapin to codex --print"
Sleep 500ms
Enter
Sleep 8s
