"""Native Astra directory resolver; this module never reads authentication tokens."""
import os
import sys
from pathlib import Path


def config_dir() -> Path:
    from .platform_support import astra_config
    return astra_config()
