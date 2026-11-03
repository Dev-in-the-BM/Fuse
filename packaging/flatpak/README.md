# Flatpak build

The manifest targets GNOME Platform 51 (Python 3.14) and uses the locally
available GNOME SDK 50 to assemble the app without compiling dependencies. It
packages the **current worktree**
(including source edits), plus the local MTK Client checkout, while excluding
its `.git`, `.venv`, Python cache directories, and non-runtime app assets. The
Flatpak builder itself is kept under `$HOME/.local/share/fuse/flatpak-build`.
Build with:

```sh
python3 packaging/flatpak/build.py
```

The build uses `uv` and its shared cache to install the filtered upstream CLI
requirements for Python 3.14, omitting only the `PySide6*` and `shiboken6`
packages. Dependency preparation is offline; if the cache does not contain a
required wheel, first populate it with a normal `uv pip install` using the
generated `mtkclient/requirements.txt` minus those Qt entries. It also bundles
Ubuntu 22.04's `libfuse2` package (verified by SHA-256) because MTK Client's
optional filesystem-mount module loads it at import time. The backend source
and dependencies are included in the artifact, so first launch does not need
to download or install the backend. The build output is written next to the
repository as `quest.devinthebm.fuse_<version>_x86_64.flatpak`.

The app needs broad USB device access (`--device=all`) to communicate with
MediaTek devices. That Flatpak permission does not install host udev rules;
device nodes may still need a host-side udev rule granting the logged-in user
access. The app's existing USB setup action should not be assumed to configure
host rules when sandboxed.

The bundle is built but not installed by this script. To install it for the
current user after reviewing it:

```sh
flatpak install --user --bundle ../quest.devinthebm.fuse_<version>_x86_64.flatpak
```
