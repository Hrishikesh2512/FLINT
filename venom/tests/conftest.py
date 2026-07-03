"""Test isolation: never let a real override file leak into config tests."""

import pytest


@pytest.fixture(autouse=True)
def _no_override_file(tmp_path, monkeypatch):
    monkeypatch.setenv("VENOM_OVERRIDE", str(tmp_path / "no-override.toml"))
