#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SYSTEM_PYTHON="${SYSTEM_PYTHON:-/usr/bin/python3}"
SAVED_MTKCLIENT_DIR="$(APP_DIR="$APP_DIR" PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" "$SYSTEM_PYTHON" -c 'import os; from pathlib import Path; from app_settings import get_backend_dir; from backend_manager import default_backend_dir, is_backend_checkout; app = Path(os.environ["APP_DIR"]); candidates = (get_backend_dir(), default_backend_dir(), app / "mtkclient", app.parent / "mtkclient"); print(next((str(p) for p in candidates if p and is_backend_checkout(p)), str(default_backend_dir())))' 2>/dev/null || true)"
MTKCLIENT_DIR="${MTKCLIENT_DIR:-${SAVED_MTKCLIENT_DIR:-$APP_DIR/../mtkclient}}"
VENV="$APP_DIR/.venv"

if [[ -f "$VENV/bin/activate" ]]; then
    # Reuse the app's development environment when it exists.
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
fi

# PyGObject is supplied by the system GTK packages, not PyPI. Append the distro
# package directory after the selected environment's packages via sitecustomize.
# Prepending it through PYTHONPATH can shadow matching native extensions.
GI_PATH="$("$SYSTEM_PYTHON" -c 'import gi, os; print(os.path.dirname(os.path.dirname(gi.__file__)))' 2>/dev/null || true)"
if [[ -n "$GI_PATH" ]]; then
    export MTK_GI_PATH="$GI_PATH"
    export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"
else
    echo "Could not find system PyGObject. Install python3-gi, gir1.2-gtk-4.0, and gir1.2-adw-1." >&2
    exit 1
fi

if [[ -f "$MTKCLIENT_DIR/pyproject.toml" && -f "$MTKCLIENT_DIR/uv.lock" ]]; then
    MTKCLIENT_DIR="$(cd -- "$MTKCLIENT_DIR" && pwd)"
    export MTKCLIENT_DIR
    cd "$MTKCLIENT_DIR"
    if command -v uv >/dev/null 2>&1; then
        exec uv run --active --project "$MTKCLIENT_DIR" python "$APP_DIR/mtk_adwaita.py" "$@"
    elif [[ -x "$MTKCLIENT_DIR/.venv/bin/python" ]]; then
        exec "$MTKCLIENT_DIR/.venv/bin/python" "$APP_DIR/mtk_adwaita.py" "$@"
    fi
    echo "No uv or backend .venv found. Install the backend from Preferences." >&2
    exit 1
fi

echo "MTK Client checkout not configured or not found at: $MTKCLIENT_DIR" >&2
echo "The app will open so you can choose its location in Preferences." >&2
cd "$APP_DIR"
exec "$SYSTEM_PYTHON" "$APP_DIR/mtk_adwaita.py" "$@"
