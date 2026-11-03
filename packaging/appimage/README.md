# AppImage build (work in progress)

The build uses Ubuntu 24.04 in Docker as the ABI baseline, and keeps the large
generated AppDir under `$HOME/.local/share/fuse/appimage-build` rather than in
the source tree or a temporary directory. The official AppImage tooling is
stored persistently under `$HOME/.local/share/fuse/appimage-tools`.

Start a build with:

```sh
packaging/appimage/build.sh
```

The script packages the current worktree and local `mtkclient` checkout,
omitting the backend's Qt GUI requirements, its `.venv`, and non-runtime app
assets. Build output is versioned in `dist/`. Core GLib/GIO are deliberately
taken from the host to avoid mixing old runtime libraries with newer host
modules. The application reached its startup screen in the development-host
smoke test; clean-host compatibility and physical USB access remain untested.
