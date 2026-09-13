# Releasing Tap In

Pushing a `v*` tag runs `.github/workflows/release.yml`. It checks that the tag matches the package version, runs the tests, builds with `uv build`, and publishes to PyPI with trusted publishing, so no API token is stored anywhere.

## First release

1. **Choose a license and add it.** Add a `LICENSE` file, then set `license = "<SPDX expression>"` and `license-files = ["LICENSE"]` in `pyproject.toml`.
2. **Set up trusted publishing.** On PyPI, add a pending publisher for the project `tapin` with owner `andkhong`, repository `tapin`, workflow `release.yml` and environment `pypi`. In the GitHub repository settings, create an environment named `pypi`.
3. **Bump the version** in `pyproject.toml` and `src/tapin/__init__.py`. `tests/test_package.py` fails if they differ.
4. **Tag and push the tag:**

   ```sh
   git tag v0.1.0
   git push origin v0.1.0
   ```

5. **Point installs at PyPI.** Once `tapin` is on PyPI, set `DEFAULT_SPEC="tapin"` in `install.sh`, and change the README's uv line to `uv tool install tapin && tapin install`.

## Later releases

Repeat steps 3 and 4.
