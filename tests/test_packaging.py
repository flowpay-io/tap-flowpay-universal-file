"""The renamed package keeps the old executable so existing pipeline configurations keep running."""

from __future__ import annotations

import pathlib
import re

PYPROJECT = (pathlib.Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")


def scripts() -> dict[str, str]:
    section = PYPROJECT.split("[tool.poetry.scripts]", 1)[1].split("\n[", 1)[0]
    return dict(re.findall(r"^([\w-]+)\s*=\s*['\"]([^'\"]+)['\"]", section, re.MULTILINE))


def test_new_and_legacy_executables_run_the_same_tap():
    entry = "tap_flowpay_universal_file.tap:TapFlowpayUniversalFile.cli"
    assert scripts() == {"tap-flowpay-universal-file": entry, "tap-gokasa": entry}


def test_package_is_published_under_the_new_name():
    assert re.search(r'^name = "tap-flowpay-universal-file"$', PYPROJECT, re.MULTILINE)
    assert '{ include = "tap_flowpay_universal_file" }' in PYPROJECT
