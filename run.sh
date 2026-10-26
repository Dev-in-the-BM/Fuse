#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname -- "$APP_DIR")"
VENV="$PROJECT_DIR/.venv"

if [[ -f "$VENV/bin/activate" ]]; then
    # Reuse the repository's development environment when it exists.
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required to launch the MTK Client Adwaita app." >&2
    exit 1
fi

# PyGObject is supplied by the system GTK packages, not PyPI. Make the system
# GI bindings visible to the activated Python environment while uv resolves
# and runs the app with the project's locked dependencies.
# Use the OS interpreter explicitly: after activation, `python3` resolves to
# the isolated venv, which cannot import the distro-provided PyGObject module.
SYSTEM_PYTHON="${SYSTEM_PYTHON:-/usr/bin/python3}"
GI_PATH="$("$SYSTEM_PYTHON" -c 'import gi, os; print(os.path.dirname(os.path.dirname(gi.__file__)))' 2>/dev/null || true)"
if [[ -n "$GI_PATH" ]]; then
    export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$GI_PATH"
else
    echo "Could not find system PyGObject. Install python3-gi, gir1.2-gtk-4.0, and gir1.2-adw-1." >&2
    exit 1
fi

cd "$PROJECT_DIR"
exec uv run --active --project "$PROJECT_DIR/mtkclient" python "$APP_DIR/mtk_adwaita.py" "$@"
