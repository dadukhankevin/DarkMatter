"""Keep test passports and collaboration sessions out of the user's live network."""

import pytest


@pytest.fixture(autouse=True)
def isolated_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKMATTER_NEARBY_DIR", str(tmp_path / "test-nearby"))
    monkeypatch.setenv("DARKMATTER_LOCAL_DIR", str(tmp_path / "test-local"))
