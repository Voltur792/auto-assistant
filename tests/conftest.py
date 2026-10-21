"""Keep every offline test away from the user's passwords and Astra files."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import store


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    plugin_dir = tmp_path / "plugin-data"
    monkeypatch.setenv("AA_DATA_DIR", str(plugin_dir))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(store, "DATA_DIR", plugin_dir)
    monkeypatch.setattr(store, "SETTINGS_FILE", plugin_dir / "settings.json")
    monkeypatch.setattr(store, "STATE_FILE", plugin_dir / "state.json")
    monkeypatch.setattr(store, "_migrated", True)
