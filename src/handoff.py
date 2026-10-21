"""Native Astra directory resolver; this module never reads authentication tokens."""
import os
import sys
from pathlib import Path


def config_dir() -> Path:
    # Same directories::ProjectDirs("com", "astra", "astra") as Astra.
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "astra/astra/config"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not base.is_absolute():
        base = Path.home() / ".config"  # XDG ignores relative environment paths.
    return base / "astra"
