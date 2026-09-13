import tomllib
from pathlib import Path

import tapin

PROJECT = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]


def test_version_matches_pyproject():
    assert tapin.__version__ == PROJECT["version"]


def test_no_runtime_dependencies():
    assert PROJECT["dependencies"] == []
