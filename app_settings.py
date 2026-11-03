"""Persistent per-user settings shared by the launcher and GTK application."""

import json
import os
from pathlib import Path


APP_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mtkclient-adwaita"
SETTINGS_FILE = APP_CONFIG_DIR / "settings.json"
DEFAULT_BACKEND_LOG_LIMIT_MB = 50
MIN_BACKEND_LOG_LIMIT_MB = 50
MAX_BACKEND_LOG_LIMIT_MB = 2048


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


def get_backend_log_limit_mb():
    value = load_settings().get("backend_log_limit_mb", DEFAULT_BACKEND_LOG_LIMIT_MB)
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_BACKEND_LOG_LIMIT_MB
    return min(MAX_BACKEND_LOG_LIMIT_MB, max(MIN_BACKEND_LOG_LIMIT_MB, value))


def get_pinned_partitions():
    value = load_settings().get("pinned_partitions", [])
    if not isinstance(value, list):
        return []
    return [str(name) for name in value if isinstance(name, str)]


def get_firmware_image_directory():
    value = load_settings().get("firmware_image_directory")
    return str(Path(value).expanduser()) if isinstance(value, str) and value else None


def get_auto_identify_partitions(default=False):
    return bool(load_settings().get("auto_identify_partitions", default))


def _save_settings(settings):
    APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    temporary.replace(SETTINGS_FILE)


def set_advanced_logging(enabled):
    settings = load_settings()
    settings["advanced_logging"] = bool(enabled)
    _save_settings(settings)


def set_backend_log_limit_mb(limit_mb):
    if isinstance(limit_mb, bool) or not isinstance(limit_mb, int):
        raise ValueError("Backend log limit must be an integer number of MiB")
    if not MIN_BACKEND_LOG_LIMIT_MB <= limit_mb <= MAX_BACKEND_LOG_LIMIT_MB:
        raise ValueError(
            f"Backend log limit must be between {MIN_BACKEND_LOG_LIMIT_MB} "
            f"and {MAX_BACKEND_LOG_LIMIT_MB} MiB"
        )
    settings = load_settings()
    settings["backend_log_limit_mb"] = limit_mb
    _save_settings(settings)


def set_backend_dir(path):
    settings = load_settings()
    settings["backend_dir"] = str(Path(path).expanduser().resolve())
    _save_settings(settings)


def set_pinned_partitions(names):
    settings = load_settings()
    settings["pinned_partitions"] = sorted(
        {str(name) for name in names if str(name)}, key=str.casefold
    )
    _save_settings(settings)


def set_firmware_image_directory(path):
    settings = load_settings()
    settings["firmware_image_directory"] = str(Path(path).expanduser().resolve())
    _save_settings(settings)


def set_auto_identify_partitions(enabled):
    settings = load_settings()
    settings["auto_identify_partitions"] = bool(enabled)
    _save_settings(settings)
