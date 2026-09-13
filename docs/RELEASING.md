# Releasing Tap In

Pushing a `v*` tag runs `.github/workflows/release.yml`. It checks that the tag matches the package version, runs the tests, builds with `uv build`, and publishes to PyPI with trusted publishing, so no API token is stored anywhere.

## First release

Tap In is MIT licensed; `pyproject.toml` declares it with `license = "MIT"` and `license-files = ["LICENSE"]`.

1. **Set up trusted publishing.** On PyPI, add a pending publisher for the project `tapin` with owner `andkhong`, repository `tapin`, workflow `release.yml` and environment `pypi`. In the GitHub repository settings, create an environment named `pypi`.
2. **Bump the version, if needed.** Run `uv version <new version>`, which updates `pyproject.toml` and `uv.lock` (the release workflow runs the tests with `--locked`, so a stale lock fails it). Then set the same version in `src/tapin/__init__.py`; `tests/test_package.py` fails if they differ. Commit both.
3. **Tag and push the tag.** The tag must be `v` plus the version that `uv version --short` prints:

   ```sh
   git tag v0.1.0
   git push origin v0.1.0
   ```

4. **Point installs at PyPI.** Once `tapin` is on PyPI, set `DEFAULT_SPEC="tapin"` in `install.sh`, and change the README's uv line to `uv tool install tapin && tapin install`.

## Later releases

Repeat steps 2 and 3.
