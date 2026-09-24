"""Keep test passports and collaboration sessions out of the user's live network."""

import pytest


@pytest.fixture(autouse=True)
def isolated_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKMATTER_NEARBY_DIR", str(tmp_path / "test-nearby"))
    monkeypatch.setenv("DARKMATTER_LOCAL_DIR", str(tmp_path / "test-local"))
    # Never touch a live repo space under ~/.darkmatter/spaces; tests opt in explicitly.
    monkeypatch.setenv("DARKMATTER_SPACE_DIR", str(tmp_path / "test-space"))
    monkeypatch.setenv("DARKMATTER_SPACE_SYNC_SECONDS", "0")
    # Tests never announce on the real network; network tests run loopback nodes explicitly.
    monkeypatch.setenv("DARKMATTER_NETWORK_MODE", "off")
