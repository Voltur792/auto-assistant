from src import platform_support as paths
from src import handoff, store


def test_mac_profile_ignores_windows_appdata(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", "unused-windows-profile")
    monkeypatch.delenv("AA_DATA_DIR", raising=False)
    monkeypatch.delenv("ASTRA_CONFIG_DIR", raising=False)
    assert store._default_data_dir() == tmp_path / "Library/Application Support/auto-assistant"
    assert handoff.config_dir() == tmp_path / "Library/Application Support/com.astra.astra"


def test_linux_configuration_honors_absolute_xdg_and_explicit_override(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("ASTRA_CONFIG_DIR", raising=False)
    assert handoff.config_dir() == tmp_path / "xdg/astra"
    monkeypatch.setenv("ASTRA_CONFIG_DIR", str(tmp_path / "custom"))
    assert handoff.config_dir() == tmp_path / "custom"
