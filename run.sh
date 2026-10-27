#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SYSTEM_PYTHON="${SYSTEM_PYTHON:-/usr/bin/python3}"
SAVED_MTKCLIENT_DIR="$(PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" "$SYSTEM_PYTHON" -c 'from app_settings import get_backend_dir; print(get_backend_dir(""))' 2>/dev/null || true)"
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
    if ! command -v uv >/dev/null 2>&1; then
        echo "uv is required to use the selected MTK Client checkout." >&2
        exit 1
    fi
    cd "$MTKCLIENT_DIR"
    exec uv run --active --project "$MTKCLIENT_DIR" python "$APP_DIR/mtk_adwaita.py" "$@"
fi

echo "MTK Client checkout not configured or not found at: $MTKCLIENT_DIR" >&2
echo "The app will open so you can choose its location in Preferences." >&2
cd "$APP_DIR"
exec "$SYSTEM_PYTHON" "$APP_DIR/mtk_adwaita.py" "$@"
