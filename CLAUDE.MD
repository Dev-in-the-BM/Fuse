# Contributor Notes

## Project structure and backend

- The Adwaita app lives at this repository's root (`mtk_adwaita.py`); supporting files include `run.sh`, `app_settings.py`, `sitecustomize.py`, and `assets/`. Do not assume an `Adwaita/` subdirectory.
- The MTK Client checkout is a separate project selected in app Preferences. Its project directory must contain `pyproject.toml` and `uv.lock`. `MTKCLIENT_DIR` can override the saved location for a single launch.
- The selected checkout is the authoritative device backend. Do not edit or duplicate its implementation or legacy Qt GUI as part of Adwaita work.
- The selected checkout path is saved under `${XDG_CONFIG_HOME:-~/.config}/mtkclient-adwaita/settings.json`. Keep machine-specific paths out of the repository.
- `run.sh` launches against the selected checkout's locked Python environment. GTK/libadwaita and PyGObject come from system packages. `sitecustomize.py` appends the distro Python package path so system GTK bindings are available without shadowing venv packages/native extensions (notably `_cffi_backend`). Do not prepend the whole distro `dist-packages` directory.
- MTK's file logger expects a `logs/` directory under the selected checkout; create it before initializing `MtkConfig`.

## Implementation conventions

- Use Python, PyGObject, GTK 4, and libadwaita; follow GNOME HIG patterns and prefer native Adwaita widgets.
- Keep GTK work on the main thread. Run device I/O in workers and return UI updates through `GLib.idle_add`.
- Keep connection, empty, loading, progress, and error states understandable and accessible. Give icon-only controls accessible labels/tooltips.
- Gate destructive operations with explicit confirmation. Report success only when the backend confirms it; surface failures and logs rather than inferring outcomes.
- Keep goals in `GOALS.MD` short and stable. Keep `PROGRESS.MD` to a concise implementation/validation snapshot and next gaps; put durable contributor and architecture notes here.

## Validation

- Run `bash -n run.sh`, `python3 -m py_compile app_settings.py sitecustomize.py mtk_adwaita.py`, and `git diff --check` after relevant changes.
- Exercise settings persistence and backend imports with the selected checkout environment when changing launcher or dependency handling.
- Fake-backend tests and GTK construction checks do not replace connected-device testing. No device operation is release-validated until tested on supported hardware.
