# Launch checklist

What the owner does to launch Tap In, in order.

1. **Review and merge the three PRs:** native session readers, warnings before the limit, and launch prep.
2. **Install it and trust the Codex hooks.** Run the install one-liner from the README, which also runs `tapin install`. From a checkout, run `uv tool install --editable .` and then `tapin install`. Then open Codex, run `/hooks`, and trust Tap In's `SessionStart`, `Stop` and `PostToolUse` hooks.
3. **Run `tapin doctor`** and fix anything it marks `FAIL` or `warn`.
4. **Set up PyPI trusted publishing.** On PyPI, add a pending publisher for the project `tapin`: owner `andkhong`, repository `tapin`, workflow `release.yml`, environment `pypi`. In the GitHub repository settings, create an environment named `pypi`.
5. **Tag the release.** Bump the version if needed ([RELEASING.md](RELEASING.md) says how), then tag `v0.1.0` and push the tag:

   ```sh
   git tag v0.1.0
   git push origin v0.1.0
   ```

   The release workflow checks that the tag matches the package version, runs the tests, builds, and publishes to PyPI. Once it's out, point the installer and README at PyPI (step 4 of RELEASING.md).
6. **Record the demo GIF** with [DEMO.md](DEMO.md) and add it to the README.
7. **Post a Show HN, and on Reddit in r/ClaudeAI and r/codex.** Lead with the warning before the limit and with handing off in any direction between Claude Code, Codex and Cursor. Link the README's [Limitations](../README.md#limitations) and say them upfront: Codex limit capture hasn't been observed live, Claude Code warnings need the status line and a Pro or Max plan, Cursor captures on any agent error, and Windows isn't supported.
