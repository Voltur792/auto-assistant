"""SDK protocol and icon checks."""
from pathlib import Path
from astra_plugin_sdk.testing import Harness, fuzz_configs
from src.plugin import AstraAutoAssistant


def test_the_plugin_starts_and_answers_a_health_check():
    with Harness(AstraAutoAssistant()) as h:
        healthy, _status = h.health()
        assert healthy


def test_no_config_the_daemon_can_deliver_crashes_this_plugin():
    # The daemon delivers config it did not author: the user's typing, and an
    # older version of this plugin's own schema. `{}` — a fresh install — is
    # the first payload every plugin ever sees. None of it may throw.
    with Harness(AstraAutoAssistant()) as h:
        for payload in fuzz_configs():
            h.set_config(payload)


def test_header_icon_is_available_outside_ui_asset_route():
    import asyncio
    import base64
    icon = Path(__file__).resolve().parent.parent / "icon.png"
    result = asyncio.run(AstraAutoAssistant().ui_get_icon())
    assert result["data_uri"].startswith("data:image/png;base64,")
    assert base64.b64decode(result["data_uri"].split(",", 1)[1]) == icon.read_bytes()
    assert icon.stat().st_size <= 8192
