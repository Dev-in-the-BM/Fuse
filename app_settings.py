"""Persistent per-user settings shared by the launcher and GTK application."""

import json
import os
from pathlib import Path


APP_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mtkclient-adwaita"
SETTINGS_FILE = APP_CONFIG_DIR / "settings.json"


def load_settings():
    try:
        with SETTINGS_FILE.open(encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def get_backend_dir(default=None):
    return load_settings().get("backend_dir") or default


def set_backend_dir(path):
    APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"backend_dir": str(Path(path).expanduser().resolve())}, indent=2) + "\n",
                                    encoding="utf-8")
    temporary.replace(SETTINGS_FILE)
