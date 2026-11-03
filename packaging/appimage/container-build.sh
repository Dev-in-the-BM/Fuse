#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export APPIMAGE_EXTRACT_AND_RUN=1
mkdir -p /build/bin
ln -sf /tools/appimagetool-x86_64.AppImage /build/bin/appimagetool
export PATH="/build/bin:$PATH"

apt-get update -qq
apt-get install -yqq --no-install-recommends \
	ca-certificates python3 python3.12 libpython3.12 python3-gi python3-gi-cairo python3-cairo \
	python3-pip gir1.2-gtk-4.0 gir1.2-adw-1 \
	libgtk-4-1 libadwaita-1-0 libfuse2 binutils file patchelf \
	libgirepository-1.0-1 libglib2.0-0 libusb-1.0-0

APPDIR=/build/AppDir
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib/python3.12" \
	"$APPDIR/usr/lib/python3/dist-packages" \
	"$APPDIR/usr/lib/python3.12/site-packages" \
	"$APPDIR/usr/share/fuse" "$APPDIR/usr/share/applications" \
	"$APPDIR/usr/share/icons/hicolor/scalable/apps" "$APPDIR/usr/lib"

cp -a /usr/bin/python3.12 "$APPDIR/usr/bin/python3"
cp -a /usr/lib/python3.12/. "$APPDIR/usr/lib/python3.12/"
cp -a /usr/lib/python3/dist-packages/gi "$APPDIR/usr/lib/python3/dist-packages/"
cp -a /usr/lib/python3/dist-packages/gi*.dist-info "$APPDIR/usr/lib/python3/dist-packages/" 2>/dev/null || true
cp -a /usr/lib/python3/dist-packages/cairo "$APPDIR/usr/lib/python3/dist-packages/"
cp -a /usr/lib/python3/dist-packages/pycairo* "$APPDIR/usr/lib/python3/dist-packages/" 2>/dev/null || true
cp -a /usr/lib/x86_64-linux-gnu/libpython3.12.so* "$APPDIR/usr/lib/"
cp -a /usr/lib/x86_64-linux-gnu/girepository-1.0 "$APPDIR/usr/lib/"
cp -a /usr/lib/x86_64-linux-gnu/libfuse.so.2* "$APPDIR/usr/lib/"
cp -a /usr/lib/x86_64-linux-gnu/libusb-1.0.so* "$APPDIR/usr/lib/"

cp -a /src/mtkclient "$APPDIR/usr/share/fuse/mtkclient"
rm -rf "$APPDIR/usr/share/fuse/mtkclient/.git" \
	"$APPDIR/usr/share/fuse/mtkclient/.venv"
find "$APPDIR/usr/share/fuse/mtkclient" -type d -name __pycache__ -prune -exec rm -rf {} +
cp /src/app_settings.py /src/backend_manager.py /src/bounded_logging.py \
	/src/mtk_adwaita.py /src/mtk_image_identify.py /src/sitecustomize.py \
	"$APPDIR/usr/share/fuse/"
while IFS= read -r asset; do
	[ -n "$asset" ] || continue
	mkdir -p "$APPDIR/usr/share/fuse/$(dirname "$asset")"
	cp "/src/$asset" "$APPDIR/usr/share/fuse/$asset"
done < /src/packaging/runtime-assets.txt

python3 - <<'PY'
import sys
sys.path.insert(0, '/src')
from backend_manager import filter_cli_requirements
from pathlib import Path
requirements = filter_cli_requirements(Path('/src/mtkclient/requirements.txt').read_text())
Path('/build/backend-requirements.txt').write_text(requirements)
PY
python3 -m pip install --disable-pip-version-check --no-warn-script-location \
	--target "$APPDIR/usr/lib/python3.12/site-packages" \
	-r /build/backend-requirements.txt

cp /src/debian/quest.devinthebm.fuse.desktop "$APPDIR/usr/share/applications/"
cp /src/assets/fuse-mtk-app-icon.svg \
	"$APPDIR/usr/share/icons/hicolor/scalable/apps/quest.devinthebm.fuse.svg"
cp /src/assets/fuse-mtk-app-icon-512.png "$APPDIR/quest.devinthebm.fuse.png"

cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/sh
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONHOME="$HERE/usr"
export PYTHONPATH="$HERE/usr/share/fuse:$HERE/usr/lib/python3.12/site-packages:$HERE/usr/lib/python3/dist-packages"
export GI_TYPELIB_PATH="$HERE/usr/lib/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
export LD_LIBRARY_PATH="$HERE/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export FUSE_LIBRARY_PATH="$HERE/usr/lib/libfuse.so.2"
exec "$HERE/usr/bin/python3" "$HERE/usr/share/fuse/mtk_adwaita.py" "$@"
APPRUN
chmod 755 "$APPDIR/AppRun"

cat > "$APPDIR/usr/bin/fuse" <<'LAUNCHER'
#!/bin/sh
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
exec "$HERE/AppRun" "$@"
LAUNCHER
chmod 755 "$APPDIR/usr/bin/fuse"

"/tools/linuxdeploy-x86_64.AppImage" --appdir "$APPDIR" \
	--executable "$APPDIR/usr/bin/python3" \
	--desktop-file "$APPDIR/usr/share/applications/quest.devinthebm.fuse.desktop" \
	--icon-file "$APPDIR/usr/share/icons/hicolor/scalable/apps/quest.devinthebm.fuse.svg" \
	--library /usr/lib/x86_64-linux-gnu/libgtk-4.so.1 \
	--library /usr/lib/x86_64-linux-gnu/libadwaita-1.so.0 \
	--library /usr/lib/x86_64-linux-gnu/libpango-1.0.so.0 \
	--library /usr/lib/x86_64-linux-gnu/libpangocairo-1.0.so.0 \
	--library /usr/lib/x86_64-linux-gnu/libpangoft2-1.0.so.0 \
	--library /usr/lib/x86_64-linux-gnu/libgdk_pixbuf-2.0.so.0 \
	--library /usr/lib/x86_64-linux-gnu/libepoxy.so.0 \
	--library /usr/lib/x86_64-linux-gnu/libgraphene-1.0.so.0 \
	--deploy-deps-only "$APPDIR/usr/lib"

# Keep core GLib/GIO/GObject from the host. Bundling Jammy's older GLib while
# loading host GIO/GTK modules breaks on newer distros (missing symbols).
rm -f "$APPDIR"/usr/lib/libglib-2.0.so* \
	"$APPDIR"/usr/lib/libgio-2.0.so* \
	"$APPDIR"/usr/lib/libgobject-2.0.so* \
	"$APPDIR"/usr/lib/libgmodule-2.0.so* \
	"$APPDIR"/usr/lib/libgirepository-1.0.so* \
	"$APPDIR"/usr/lib/libmount.so*

"/tools/appimagetool-x86_64.AppImage" "$APPDIR" \
	/build/quest.devinthebm.fuse-x86_64.AppImage
