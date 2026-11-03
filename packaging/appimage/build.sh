#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
FUSE_HOME="$DATA_HOME/fuse"
TOOLS="$FUSE_HOME/appimage-tools"
BUILD="$FUSE_HOME/appimage-build"

for tool in appimagetool-x86_64.AppImage linuxdeploy-x86_64.AppImage; do
	if [ ! -x "$TOOLS/$tool" ]; then
		echo "Missing persistent AppImage tool: $TOOLS/$tool" >&2
		exit 1
	fi
done
command -v docker >/dev/null 2>&1 || { echo "Docker is required" >&2; exit 1; }
mkdir -p "$BUILD/logs" "$ROOT/dist"

if ! docker run --rm \
	-v "$ROOT:/src:ro" \
	-v "$BUILD:/build" \
	-v "$TOOLS:/tools:ro" \
	ubuntu:24.04 /bin/bash /src/packaging/appimage/container-build.sh \
	>"$BUILD/logs/build.log" 2>&1; then
	tail -n 80 "$BUILD/logs/build.log" >&2
	exit 1
fi

PACKAGE_VERSION=$(dpkg-parsechangelog -l "$ROOT/debian/changelog" -S Version)
APP_VERSION=$(printf '%s\n' "$PACKAGE_VERSION" | sed -E 's/-[0-9]+$//; s/~/-/g')
OUTPUT="$ROOT/dist/quest.devinthebm.fuse_${APP_VERSION}_x86_64.AppImage"
if [ -f "$BUILD/quest.devinthebm.fuse-x86_64.AppImage" ]; then
	cp "$BUILD/quest.devinthebm.fuse-x86_64.AppImage" "$OUTPUT"
	chmod +x "$OUTPUT"
	echo "Created $OUTPUT"
else
	echo "AppImage assembly did not produce an artifact; staged files remain in $BUILD/AppDir" >&2
	exit 1
fi
