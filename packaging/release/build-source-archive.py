#!/usr/bin/env python3
"""Create a source-only release ZIP, excluding packaging and development files."""

from __future__ import annotations

import io
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def manifest(path: Path) -> list[str]:
    return [
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: build-source-archive.py VERSION COMMIT", file=sys.stderr)
        return 2
    version, commit = sys.argv[1:]
    root_name = f"Fuse-{version}"
    files = manifest(ROOT / "packaging/release/source-files.txt")
    assets = manifest(ROOT / "packaging/runtime-assets.txt")
    icons = [
        f"assets/fuse-mtk-app-icon-{size}.png"
        for size in (16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512)
    ] + ["assets/fuse-mtk-app-icon.svg"]
    files = list(dict.fromkeys((*files, *assets, *icons)))
    archive = subprocess.check_output(
        ["git", "archive", "--format=tar", f"--prefix={root_name}/", commit,
         *files, *assets, *icons],
        cwd=ROOT,
    )
    output = ROOT / "dist" / f"{root_name}-source.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as target:
            for member in source.getmembers():
                if not member.isfile():
                    continue
                stream = source.extractfile(member)
                if stream is None:
                    raise RuntimeError(f"Cannot read archived file {member.name}")
                target.writestr(member.name, stream.read())
            readme = (ROOT / "packaging/release/source-README.md").read_bytes()
            target.writestr(f"{root_name}/README.md", readme)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
