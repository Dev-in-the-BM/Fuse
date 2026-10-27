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


def get_advanced_logging(default=False):
    return bool(load_settings().get("advanced_logging", default))


def set_advanced_logging(enabled):
    settings = load_settings()
    settings["advanced_logging"] = bool(enabled)
    APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    temporary.replace(SETTINGS_FILE)


def set_backend_dir(path):
    settings = load_settings()
    settings["backend_dir"] = str(Path(path).expanduser().resolve())
    APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    temporary.replace(SETTINGS_FILE)
