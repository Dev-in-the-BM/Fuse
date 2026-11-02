"""Content-only indexing and progressive identification of firmware images.

Android sparse DONT_CARE extents deliberately have no byte value here: they
are excluded from fingerprints and comparisons rather than treated as zeroes.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import struct
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

COARSE = 1024 * 1024
FINE = 4096
SPARSE_MAGIC = 0xED26FF3A
RAW, FILL, DONT_CARE, CRC32 = 0xCAC1, 0xCAC2, 0xCAC3, 0xCAC4


def _image_files(directory, progress=None, cancel=None):
    """Discover eligible images recursively beneath the selected root."""
    root = Path(directory)
    files, pending = [], [root]
    scanned_directories = entries_seen = 0
    last_report = 0.0
    while pending:
        if cancel and cancel.is_set():
            raise InterruptedError("directory scan cancelled")
        current = pending.pop()
        scanned_directories += 1
        # Report before opening/enumerating the directory, so one slow or
        # remote directory cannot leave the UI on its initial selection text.
        now = time.monotonic()
        if progress and (scanned_directories <= 32 or scanned_directories % 64 == 0
                         or now - last_report >= 0.25):
            last_report = now
            progress(current, len(files), scanned_directories)
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if cancel and cancel.is_set():
                        raise InterruptedError("directory scan cancelled")
                    entries_seen += 1
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in {".img", ".bin"}:
                        files.append(Path(entry.path))
                    now = time.monotonic()
                    if entries_seen % 128 == 0 or now - last_report >= 0.25:
                        last_report = now
                        if progress:
                            progress(current, len(files), scanned_directories)
        except OSError:
            raise
    return sorted(files, key=lambda path: str(path))


@dataclass(frozen=True)
class Extent:
    start: int
    length: int
    kind: str
    source: int = 0
    pattern: bytes = b""


@dataclass(frozen=True)
class DaDiagnosticReport:
    status: str
    partition: str
    image: str
    checks: int
    matching_checks: int
    da_requests: int
    da_bytes_requested: int
    usb_read_calls: int | None
    usb_bytes_received: int | None
    elapsed_seconds: float
    read_latencies: tuple[float, ...]
    logical_offsets: tuple[int, ...]
    device_addresses: tuple[int, ...]
    exact_offsets_ok: bool
    alignment_ok: bool
    storage_region_ok: bool
    session_stable: bool
    error: str | None = None


class Image:
    def __init__(self, path: str, extents: list[Extent], size: int):
        self.path, self.extents, self.size = path, extents, size
        stat = os.stat(path)
        self.identity = (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)

    def unchanged(self):
        try:
            stat = os.stat(self.path)
        except OSError:
            return False
        return self.identity == (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)

    @classmethod
    def open(cls, path: str):
        with open(path, "rb") as f:
            header = f.read(28)
        if len(header) >= 4 and struct.unpack_from("<I", header)[0] == SPARSE_MAGIC:
            if len(header) < 28:
                raise ValueError("truncated Android sparse header")
            _, major, _minor, file_hdr, chunk_hdr, block, _total_blocks, chunks, _crc = struct.unpack("<IHHHHIIII", header)
            if major != 1 or block <= 0 or file_hdr < 28 or chunk_hdr < 12:
                raise ValueError("unsupported Android sparse format")
            extents, logical, pos = [], 0, file_hdr
            source_size = os.path.getsize(path)
            if file_hdr > source_size:
                raise ValueError("truncated sparse file header")
            with open(path, "rb") as f:
                for _ in range(chunks):
                    f.seek(pos)
                    raw = f.read(chunk_hdr)
                    if len(raw) < 12:
                        raise ValueError("truncated sparse chunk header")
                    kind, _reserved, blocks, total = struct.unpack_from("<HHII", raw)
                    length = blocks * block
                    data_pos = pos + chunk_hdr
                    if total < chunk_hdr:
                        raise ValueError("invalid sparse chunk size")
                    if kind == RAW:
                        if total != chunk_hdr + length:
                            raise ValueError("invalid RAW sparse chunk")
                        if data_pos + length > source_size:
                            raise ValueError("truncated RAW sparse data")
                        extent = Extent(logical, length, "raw", data_pos)
                    elif kind == FILL:
                        if total != chunk_hdr + 4:
                            raise ValueError("invalid FILL sparse chunk")
                        f.seek(data_pos)
                        pattern = f.read(4)
                        if len(pattern) != 4:
                            raise ValueError("truncated FILL sparse chunk")
                        extent = Extent(logical, length, "fill", pattern=pattern)
                    elif kind == DONT_CARE:
                        if total != chunk_hdr:
                            raise ValueError("invalid DONT_CARE sparse chunk")
                        extent = Extent(logical, length, "undefined")
                    elif kind == CRC32:
                        if blocks or total != chunk_hdr + 4:
                            raise ValueError("invalid sparse CRC chunk")
                        pos = data_pos + 4
                        continue
                    else:
                        raise ValueError(f"unsupported sparse chunk type 0x{kind:04x}")
                    extents.append(extent)
                    logical += length
                    pos = data_pos + total - chunk_hdr
                    if pos > source_size:
                        raise ValueError("truncated sparse chunk data")
            if logical != _total_blocks * block:
                raise ValueError("sparse logical size does not match its header")
            return cls(os.path.realpath(path), extents, logical)
        size = os.path.getsize(path)
        return cls(os.path.realpath(path), [Extent(0, size, "raw", 0)], size)

    def read(self, offset: int, length: int) -> bytes | None:
        """Return a defined logical byte range, or None if it intersects a hole."""
        if offset < 0 or length < 0 or offset + length > self.size:
            return None
        result, cursor, end = bytearray(), offset, offset + length
        for extent in self.extents:
            left, right = max(cursor, extent.start), min(end, extent.start + extent.length)
            if left >= right:
                continue
            if left != cursor or extent.kind == "undefined":
                return None
            count = right - left
            if extent.kind == "raw":
                with open(self.path, "rb") as f:
                    f.seek(extent.source + left - extent.start)
                    data = f.read(count)
                if len(data) != count:
                    return None
                result.extend(data)
            else:
                pattern = extent.pattern
                phase = (left - extent.start) % len(pattern)
                repeated = (pattern * ((count + len(pattern) + phase - 1) // len(pattern) + 1))[phase:phase + count]
                result.extend(repeated)
            cursor = right
            if cursor == end:
                return bytes(result)
        return bytes(result) if cursor == end else None

    def defined_slices(self, offset: int, length: int, stream=None):
        """Yield (logical_offset, bytes) runs, omitting DONT_CARE bytes."""
        end = min(self.size, offset + length)
        if offset < 0 or length < 0 or offset >= self.size:
            return
        for extent in self.extents:
            left, right = max(offset, extent.start), min(end, extent.start + extent.length)
            if left >= right or extent.kind == "undefined":
                continue
            count = right - left
            if extent.kind == "raw":
                owned = stream is None
                input_file = open(self.path, "rb") if owned else stream
                try:
                    input_file.seek(extent.source + left - extent.start)
                    data = input_file.read(count)
                finally:
                    if owned:
                        input_file.close()
                if len(data) != count:
                    continue
            else:
                pattern = extent.pattern
                phase = (left - extent.start) % len(pattern)
                data = (pattern * ((count + phase - 1) // len(pattern) + 1))[phase:phase + count]
            yield left, data


class ImageIndex:
    """Transactional SQLite content index; file paths are metadata only."""

    def __init__(self, database: str | os.PathLike):
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(database, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS images(path TEXT PRIMARY KEY, display TEXT NOT NULL,
              size INTEGER NOT NULL, logical_size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
              device INTEGER NOT NULL, inode INTEGER NOT NULL, complete INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS chunks(path TEXT NOT NULL, level INTEGER NOT NULL,
              offset INTEGER NOT NULL, length INTEGER NOT NULL, digest BLOB NOT NULL,
              PRIMARY KEY(path,level,offset,length));
        """)
        primary_key = tuple(
            row[1] for row in sorted(
                (row for row in self.db.execute("PRAGMA table_info(chunks)").fetchall() if row[5]),
                key=lambda row: row[5],
            )
        )
        if primary_key != ("path", "level", "offset", "length"):
            with self.db:
                self.db.execute("ALTER TABLE chunks RENAME TO chunks_legacy")
                self.db.execute("""CREATE TABLE chunks(path TEXT NOT NULL, level INTEGER NOT NULL,
                    offset INTEGER NOT NULL, length INTEGER NOT NULL, digest BLOB NOT NULL,
                    PRIMARY KEY(path,level,offset,length))""")
                self.db.execute("""INSERT OR IGNORE INTO chunks(path,level,offset,length,digest)
                    SELECT path,level,offset,length,digest FROM chunks_legacy""")
                self.db.execute("DROP TABLE chunks_legacy")
                # Legacy fingerprints used different refinement rules. Force
                # a content rebuild before any migrated row can participate.
                self.db.execute("UPDATE images SET complete=0")
        self.db.commit()
        self.lock = threading.RLock()

    def index_file(self, path: str, force=False, cancel=None, progress=None, refine=True):
        path = os.path.realpath(path)
        stat = os.stat(path)
        cached = self.db.execute("SELECT size,mtime_ns,device,inode,complete FROM images WHERE path=?", (path,)).fetchone()
        identity = (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino, 1)
        if not force and cached == identity:
            return False
        image = Image.open(path)
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO images VALUES(?,?,?,?,?,?,?,0)",
                            (path, os.path.basename(path), stat.st_size, image.size,
                             stat.st_mtime_ns, stat.st_dev, stat.st_ino))
            self.db.execute("DELETE FROM chunks WHERE path=?", (path,))
            with open(path, "rb") as image_stream:
                for offset in range(0, image.size, COARSE):
                    if cancel and cancel.is_set():
                        raise InterruptedError("indexing cancelled")
                    length = min(COARSE, image.size - offset)
                    for defined_offset, content in image.defined_slices(offset, length, image_stream):
                        digest = hashlib.blake2b(content, digest_size=32).digest()
                        self.db.execute("INSERT INTO chunks VALUES(?,?,?,?,?)",
                                        (path, 0, defined_offset, len(content), digest))
                    if progress:
                        progress(offset + length, image.size)
            after = os.stat(path)
            if (after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino) != identity[:4]:
                self.db.execute("DELETE FROM chunks WHERE path=?", (path,))
                self.db.execute("DELETE FROM images WHERE path=?", (path,))
                raise RuntimeError("image changed while it was being indexed")
            self.db.execute("UPDATE images SET complete=1 WHERE path=?", (path,))
        if refine:
            self.refine_directory(os.path.dirname(path), cancel=cancel)
        return True

    def index_directory(self, directory, force=False, cancel=None, progress=None,
                        byte_progress=None, discovery_progress=None):
        files = _image_files(
            directory,
            progress=discovery_progress,
            cancel=cancel,
        )
        if progress:
            progress(0, len(files), "")
        content_changed = False
        for number, path in enumerate(files, 1):
            if cancel and cancel.is_set():
                raise InterruptedError("indexing cancelled")
            content_changed = self.index_file(
                str(path), force=force, cancel=cancel, refine=False,
                progress=(lambda done, total, current=path:
                          byte_progress(current, number, len(files), done, total))
                         if byte_progress else None,
            ) or content_changed
            if progress:
                progress(number, len(files), path.name)
        # Remove stale paths for this selected folder; names do not affect matches.
        root = os.path.realpath(directory)
        if not root.endswith(os.sep):
            root += os.sep
        with self.db:
            known = self.db.execute("SELECT path FROM images").fetchall()
            for (path,) in known:
                if path.startswith(root) and not os.path.isfile(path):
                    self.db.execute("DELETE FROM chunks WHERE path=?", (path,))
                    self.db.execute("DELETE FROM images WHERE path=?", (path,))
                    content_changed = True
        if content_changed:
            self.refine_directory(directory, cancel=cancel, progress=progress)
        elif progress:
            progress(len(files), len(files), "Fingerprint cache unchanged")
        return len(files)

    def refine_directory(self, directory, cancel=None, progress=None):
        """Store 4 KiB fingerprints only inside coarse regions that differ."""
        root = os.path.realpath(directory)
        if not root.endswith(os.sep):
            root += os.sep
        rows = self.db.execute(
            "SELECT path,logical_size FROM images WHERE complete=1 ORDER BY path"
        ).fetchall()
        sizes = {path: size for path, size in rows if path.startswith(root)}
        paths = list(sizes)
        if not paths:
            return
        if progress:
            progress(0, 0, f"Comparing coarse fingerprints across {len(paths):,} images…")
        placeholders = ",".join("?" for _ in paths)
        count = len(paths)
        groups = self.db.execute(
            f"SELECT offset,length,COUNT(DISTINCT path),COUNT(DISTINCT digest) "
            f"FROM chunks WHERE level=0 AND path IN ({placeholders}) "
            "GROUP BY offset,length "
            f"HAVING COUNT(DISTINCT path) < ? OR COUNT(DISTINCT digest) > 1",
            [*paths, count],
        ).fetchall()
        regions = {offset // COARSE * COARSE for offset, _length, _present, _hashes in groups}
        ordered_regions = sorted(regions)
        fine = self.db.execute(
            f"SELECT DISTINCT path,offset FROM chunks WHERE level=1 AND path IN ({placeholders})",
            paths,
        ).fetchall()
        existing_regions = {path: set() for path in paths}
        stale_fine = []
        for path, offset in fine:
            region = offset // COARSE * COARSE
            if region in regions:
                existing_regions[path].add(region)
            else:
                stale_fine.append((path, offset))
        tasks = [(region, path) for region in ordered_regions for path in paths
                 if sizes[path] > region and region not in existing_regions[path]]
        total_chunks = sum(
            (min(COARSE, sizes[path] - region) + FINE - 1) // FINE
            for region, path in tasks
        )
        if progress:
            progress(0, total_chunks,
                     f"Refining {len(ordered_regions):,} differing regions across "
                     f"{len(tasks):,} image-region pairs…")
        with self.lock, self.db:
            for path, offset in stale_fine:
                if cancel and cancel.is_set():
                    raise InterruptedError("fingerprint refinement cancelled")
                self.db.execute("DELETE FROM chunks WHERE path=? AND level=1 AND offset=?", (path, offset))
            completed_chunks = 0
            last_report = time.monotonic()
            region_positions = {region: i for i, region in enumerate(ordered_regions, 1)}
            for task_number, (region, path) in enumerate(tasks, 1):
                if cancel and cancel.is_set():
                    raise InterruptedError("indexing cancelled")
                image = Image.open(path)
                with open(path, "rb") as stream:
                    end = min(region + COARSE, image.size)
                    for offset in range(region, end, FINE):
                        if cancel and cancel.is_set():
                            raise InterruptedError("fingerprint refinement cancelled")
                        for defined_offset, content in image.defined_slices(
                                offset, min(FINE, end - offset), stream):
                            digest = hashlib.blake2b(content, digest_size=32).digest()
                            self.db.execute("INSERT OR REPLACE INTO chunks VALUES(?,?,?,?,?)",
                                            (path, 1, defined_offset, len(content), digest))
                        completed_chunks += 1
                        now = time.monotonic()
                        if progress and (completed_chunks == 1 or completed_chunks % 64 == 0
                                         or now - last_report >= 0.25):
                            progress(
                                completed_chunks, total_chunks,
                                f"Fingerprinting image-region {task_number:,}/{len(tasks):,} · "
                                f"region {region_positions[region]:,}/{len(ordered_regions):,} · "
                                f"{os.path.relpath(path, directory)}",
                            )
                            last_report = now
            if progress:
                progress(total_chunks, total_chunks,
                         f"Fingerprint refinement complete · {len(ordered_regions):,} "
                         "differing regions")

    def images(self, directory=None, cancel=None, progress=None):
        if directory:
            # Discover newly added files as well as validating known metadata;
            # unchanged files hit the identity cache and are not rehashed.
            def indexing_progress(done, total, name):
                if not progress:
                    return
                if name.startswith("Comparing coarse"):
                    progress(name, None)
                elif name.startswith("Refining") or name.startswith("Fingerprint"):
                    fraction = done / total if total else None
                    progress(name, fraction)
                elif total:
                    progress(f"Checking image cache {done:,}/{total:,}" + (f" · {name}" if name else ""),
                             done / total * 0.2)

            def discovery_progress(path, found, directories):
                if progress:
                    progress(
                        f"Rechecking selected tree · {directories:,} folders · {found:,} image files · "
                        f"{os.path.relpath(path, directory)}",
                        None,
                    )

            def byte_progress(path, number, count, done, total):
                if progress:
                    fraction = (done / total) if total else 0
                    progress(
                        f"Reindexing {number:,}/{count:,} · {os.path.relpath(path, directory)} · "
                        f"{fraction:.0%} of file",
                        fraction,
                    )

            self.index_directory(
                directory, cancel=cancel, progress=indexing_progress,
                discovery_progress=discovery_progress, byte_progress=byte_progress,
            )
        if directory:
            root = os.path.realpath(directory)
            if not root.endswith(os.sep):
                root += os.sep
            rows = self.db.execute("SELECT path,logical_size FROM images WHERE complete=1 ORDER BY path").fetchall()
            rows = [(path, size) for path, size in rows if path.startswith(root)]
        else:
            rows = self.db.execute("SELECT path,logical_size FROM images WHERE complete=1 ORDER BY path").fetchall()
        # Metadata validation is the fallback for missed filesystem notifications.
        changed_metadata = False
        for path, _size in rows:
            if cancel and cancel.is_set():
                raise InterruptedError("matching cancelled")
            try:
                stat = os.stat(path)
            except OSError:
                with self.db:
                    self.db.execute("DELETE FROM chunks WHERE path=?", (path,))
                    self.db.execute("DELETE FROM images WHERE path=?", (path,))
                continue
            cached = self.db.execute("SELECT size,mtime_ns,device,inode FROM images WHERE path=?", (path,)).fetchone()
            if cached != (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino):
                changed_metadata = True
                if progress:
                    progress(f"Reindexing changed image · {os.path.relpath(path, directory or os.path.dirname(path))}", None)
                self.index_file(path, cancel=cancel, refine=False)
        if directory and changed_metadata:
            self.refine_directory(directory, cancel=cancel, progress=indexing_progress)
        if directory:
            rows = [(p, s) for p, s in rows if os.path.isfile(p)]
            rows = [(p, self.db.execute("SELECT logical_size FROM images WHERE path=?", (p,)).fetchone()[0]) for p, _ in rows]
        loaded = []
        for path, size in rows:
            image = Image.open(path)
            cached = self.db.execute(
                "SELECT size,mtime_ns,device,inode FROM images WHERE path=?", (path,)
            ).fetchone()
            if cached != image.identity:
                raise RuntimeError("image changed while matching was being prepared")
            loaded.append((image, size))
        return loaded

    def distinguishing_offsets(self, candidates, progress=None, cancel=None):
        """Offsets from content disagreement only; deterministic independent of path."""
        if len(candidates) < 2:
            return []
        paths = [image.path for image, _ in candidates]
        placeholders = ",".join("?" for _ in paths)
        def differing(level):
            return self.db.execute(
                f"SELECT offset,length,COUNT(DISTINCT path),COUNT(DISTINCT digest) "
                f"FROM chunks WHERE level=? AND path IN ({placeholders}) "
                "GROUP BY offset,length "
                f"HAVING COUNT(DISTINCT path) < ? OR COUNT(DISTINCT digest) > 1",
                [level, *paths, len(paths)],
            ).fetchall()

        if cancel:
            self.db.set_progress_handler(lambda: 1 if cancel.is_set() else 0, 10_000)
        try:
            if progress:
                progress("Comparing coarse image fingerprints…", None)
            coarse_rows = differing(0)
            if cancel and cancel.is_set():
                raise InterruptedError("candidate fingerprint comparison cancelled")
            # Refine only a handful of the strongest coarse regions, and retain
            # only their most discriminating fine offsets. The index may have
            # millions of 4 KiB hashes; aggregating all of them on every scan
            # is needlessly expensive when quick matching reads at most ~128 KiB.
            ranked_regions = {}
            for offset, _length, present, digests in coarse_rows:
                region = offset // COARSE * COARSE
                score = digests + len(paths) - present
                ranked_regions[region] = max(ranked_regions.get(region, 0), score)
            regions = sorted(ranked_regions, key=lambda region: (-ranked_regions[region], region))[:6]
            if progress:
                progress(
                    f"Found {len(ranked_regions):,} differing coarse regions; refining the strongest {len(regions):,}…",
                    None,
                )
            fine_offsets = []
            for region_number, region in enumerate(regions, 1):
                if cancel and cancel.is_set():
                    raise InterruptedError("candidate fingerprint comparison cancelled")
                if progress:
                    progress(
                        f"Checking fine fingerprints in differing region {region_number}/{len(regions)} "
                        f"at 0x{region:X}…", None,
                    )
                fine_offsets.extend(self.db.execute(
                    f"SELECT offset,length,COUNT(DISTINCT path),COUNT(DISTINCT digest) "
                    f"FROM chunks WHERE level=1 AND path IN ({placeholders}) "
                    "AND offset>=? AND offset<? GROUP BY offset,length "
                    f"HAVING COUNT(DISTINCT path) < ? OR COUNT(DISTINCT digest)>1 "
                    "ORDER BY COUNT(DISTINCT digest) DESC, "
                    "(COUNT(DISTINCT path) - COUNT(DISTINCT digest)) ASC, offset ASC LIMIT 4",
                    [*paths, region, region + COARSE, len(paths)],
                ).fetchall())
        except sqlite3.OperationalError as error:
            if cancel and cancel.is_set():
                raise InterruptedError("candidate fingerprint comparison cancelled") from error
            raise
        finally:
            if cancel:
                self.db.set_progress_handler(None, 0)
        return list(dict.fromkeys(
            offset for offset, _length, _present, _digests in fine_offsets
        ))


def identify(index: ImageIndex, partitions, read_at, *, full=False, cancel=None, progress=None, directory=None):
    """Match candidates against a read_at(partition, offset, length) device seam.

    partitions are mappings: name, size, and arbitrary geometry supplied to read_at.
    """
    results = []
    partition_list = list(partitions)
    partition_count = max(1, len(partition_list))
    offset_plan_cache = {}
    try:
        if progress:
            progress("", 0, "Validating indexed images…", 0.0)
        indexed = index.images(
            directory, cancel=cancel,
            progress=(lambda message, fraction: progress("", 0, message, fraction))
            if progress else None,
        )
    except InterruptedError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        return [(partition["name"], [], f"Unknown: local image indexing failed ({error})", "0 bytes")
                for partition in partition_list]
    for partition_number, partition in enumerate(partition_list, 1):
        if cancel and cancel.is_set():
            raise InterruptedError("partition scan cancelled")
        def report(message, fraction=None):
            if progress:
                progress(partition["name"], sampled, message, fraction)

        sampled = 0
        report(
            f"Partition {partition_number}/{len(partition_list)} · preparing candidates",
            (partition_number - 1) / partition_count,
        )
        candidates = [(image, size) for image, size in indexed if size <= partition["size"]]
        if not candidates:
            if any(not image.unchanged() for image, _size in indexed):
                results.append((partition["name"], [(_display_path(i.path, directory), s) for i, s in indexed],
                                "Unknown: image changed during matching", "0 bytes"))
                continue
            results.append((partition["name"], [], "No size-compatible images", "0 bytes"))
            report(
                f"Partition {partition_number}/{len(partition_list)} · no size-compatible images",
                partition_number / partition_count,
            )
            continue
        # Content-defined differences plus evenly distributed validation samples.
        report(
            f"Partition {partition_number}/{len(partition_list)} · "
            f"finding distinguishing offsets among {len(candidates):,} candidates",
            None,
        )
        if full:
            # Full streaming verification compares every defined image range;
            # building a quick-sample plan first would only add local SQL work.
            difference_offsets = []
        else:
            # Paths here are stable database row identities for memoization only;
            # they never rank or decide a content match.
            candidate_key = tuple(image.path for image, _size in candidates)
            if candidate_key in offset_plan_cache:
                difference_offsets = offset_plan_cache[candidate_key]
                report(
                    f"Partition {partition_number}/{len(partition_list)} · "
                    "reusing the content-derived sample plan",
                    None,
                )
            else:
                difference_offsets = index.distinguishing_offsets(
                    candidates,
                    progress=lambda message, fraction: report(
                        f"Partition {partition_number}/{len(partition_list)} · {message}", fraction
                    ),
                    cancel=cancel,
                )
                offset_plan_cache[candidate_key] = difference_offsets
        span = max(image.size for image, _ in candidates)
        distributed_by_path = {
            image.path: _distributed_defined_offsets(image)
            for image, _size in candidates
        } if not full else {}
        sampled, remaining, different = 0, list(candidates), set()
        coverage_compared = 0
        read_failed = False
        compared = {image.path: 0 for image, _ in candidates}
        checked_offsets = {image.path: set() for image, _ in candidates}
        device_cache = []

        def cached_device_read(offset, length):
            end, cursor, pieces, newly_read = offset + length, offset, [], 0
            for cached_offset, cached_data in sorted(device_cache):
                cached_end = cached_offset + len(cached_data)
                if cached_end <= cursor:
                    continue
                if cached_offset >= end:
                    break
                if cached_offset > cursor:
                    gap_end = min(cached_offset, end)
                    try:
                        data = read_at(partition, cursor, gap_end - cursor)
                    except Exception:
                        data = None
                    if data is None or len(data) != gap_end - cursor:
                        return None, newly_read
                    pieces.append(data)
                    device_cache.append((cursor, bytes(data)))
                    newly_read += len(data)
                    cursor = gap_end
                left, right = max(cursor, cached_offset), min(end, cached_end)
                if left < right:
                    pieces.append(cached_data[left - cached_offset:right - cached_offset])
                    cursor = right
                if cursor == end:
                    break
            if cursor < end:
                try:
                    data = read_at(partition, cursor, end - cursor)
                except Exception:
                    data = None
                if data is None or len(data) != end - cursor:
                    return None, newly_read
                pieces.append(data)
                device_cache.append((cursor, bytes(data)))
                newly_read += len(data)
            return b"".join(pieces), newly_read

        sample_number = 0

        def compare_at(offset, label, denominator):
            nonlocal sampled, remaining, coverage_compared, read_failed, sample_number
            if cancel and cancel.is_set():
                raise InterruptedError("partition scan cancelled")
            width = min(FINE, span - offset)
            applicable = [image for image, size in remaining
                          if offset < size and image.defined_slices(offset, min(width, size - offset))]
            if not applicable:
                return
            sample_number += 1
            report(
                f"Partition {partition_number}/{len(partition_list)} · {label} "
                f"{sample_number}/{denominator} at image offset 0x{offset:X}; "
                f"{len(remaining)} candidates",
                ((partition_number - 1) + (sample_number - 1) / max(1, denominator)) / partition_count,
            )
            device, newly_read = cached_device_read(offset, width)
            sampled += newly_read
            if device is None or len(device) != width:
                potential = [(_display_path(image.path, directory), size) for image, size in remaining]
                detail = getattr(read_at, "last_error", None)
                reason = f" ({detail})" if detail else ""
                results.append((partition["name"], potential,
                                f"Unknown: partition read failed{reason}",
                                f"{sampled} device bytes read"))
                remaining = []
                read_failed = True
                return
            next_remaining = []
            for image, size in remaining:
                if offset >= size:
                    next_remaining.append((image, size))
                    continue
                candidate_width = min(width, size - offset)
                slices = list(image.defined_slices(offset, candidate_width))
                if not slices:  # DONT_CARE cannot establish a result.
                    next_remaining.append((image, size))
                else:
                    matches = True
                    for defined_offset, expected in slices:
                        start = defined_offset - offset
                        if expected != device[start:start + len(expected)]:
                            matches = False
                            break
                    if matches:
                        compared[image.path] += sum(len(data) for _, data in slices)
                        coverage_compared = max(coverage_compared, compared[image.path])
                        if offset in distributed_by_path.get(image.path, ()):
                            checked_offsets[image.path].add(offset)
                        next_remaining.append((image, size))
                    else:
                        different.add(image.path)
            remaining = next_remaining
            report(
                f"Partition {partition_number}/{len(partition_list)} · {label} "
                f"complete; {len(remaining)} candidates remain; "
                f"{sampled:,} device bytes read",
                ((partition_number - 1) + sample_number / max(1, denominator)) / partition_count,
            )

        # Probe content-derived offsets first so differing candidates are
        # eliminated before spending USB reads on their distributed checks.
        # This is especially important for large image folders: checkpoints
        # for eliminated candidates must not generate extra device reads.
        if not full:
            for offset_number, offset in enumerate(difference_offsets, 1):
                if len(remaining) <= 1 or read_failed:
                    break
                if offset < span:
                    compare_at(offset, "difference-aware sample", len(difference_offsets) + 4)

            # Now validate four distributed points for each survivor. Overlaps
            # are naturally shared by the device byte cache, and offsets that
            # belonged only to eliminated candidates are skipped.
            distributed_targets = set().union(*(
                distributed_by_path[image.path] for image, _size in remaining
            )) if remaining else set()
            for offset in sorted(distributed_targets):
                if read_failed:
                    break
                if not any(offset in distributed_by_path[image.path]
                           and offset not in checked_offsets[image.path]
                           for image, _size in remaining):
                    continue
                compare_at(offset, "distributed validation", len(distributed_targets))
        if read_failed:
            continue
        if full and remaining:
            active = list(remaining)
            compared_by_path = {image.path: 0 for image, _size in active}
            failed_read = False
            max_size = max(image.size for image, _size in active)
            with ExitStack() as streams:
                local_streams = {
                    image.path: streams.enter_context(open(image.path, "rb"))
                    for image, _size in active
                }
                for offset in range(0, max_size, COARSE):
                    if cancel and cancel.is_set():
                        raise InterruptedError("verification cancelled")
                    width = min(COARSE, max_size - offset)
                    slices_by_path = {}
                    for image, _size in active:
                        if offset < image.size:
                            slices = list(image.defined_slices(
                                offset, min(width, image.size - offset), local_streams[image.path]))
                            if slices:
                                slices_by_path[image.path] = slices
                    if not slices_by_path:
                        continue
                    block_number = offset // COARSE + 1
                    block_count = (max_size + COARSE - 1) // COARSE
                    report(
                        f"Partition {partition_number}/{len(partition_list)} · full verification "
                        f"block {block_number}/{block_count} at image offset 0x{offset:X}",
                        ((partition_number - 1) + (block_number - 1) / max(1, block_count)) / partition_count,
                    )
                    ranges = sorted((logical_offset, logical_offset + len(data))
                                    for slices in slices_by_path.values()
                                    for logical_offset, data in slices)
                    merged = []
                    for start, end in ranges:
                        if merged and start <= merged[-1][1]:
                            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                        else:
                            merged.append((start, end))
                    device_runs = []
                    for start, end in merged:
                        try:
                            data = read_at(partition, start, end - start)
                        except Exception:
                            data = None
                        if data is None or len(data) != end - start:
                            failed_read = True
                            break
                        sampled += len(data)
                        device_runs.append((start, data))
                    if failed_read:
                        break
                    still_active = []
                    for image, size in active:
                        image_slices = slices_by_path.get(image.path, [])
                        if all(data == next(run_data[logical_offset - run_start:
                                                   logical_offset - run_start + len(data)]
                                            for run_start, run_data in device_runs
                                            if run_start <= logical_offset and
                                            logical_offset + len(data) <= run_start + len(run_data))
                               for logical_offset, data in image_slices):
                            compared_by_path[image.path] += sum(len(data) for _, data in image_slices)
                            still_active.append((image, size))
                        else:
                            different.add(image.path)
                    active = still_active
                    report(
                        f"Partition {partition_number}/{len(partition_list)} · verified through "
                        f"image offset 0x{offset + width:X}; {sampled:,} device bytes read",
                        ((partition_number - 1) + block_number / max(1, block_count)) / partition_count,
                    )
            if failed_read:
                potential = [(_display_path(image.path, directory), size) for image, size in active]
                detail = getattr(read_at, "last_error", None)
                reason = f" ({detail})" if detail else ""
                results.append((partition["name"], potential,
                                f"Unknown: full verification read failed{reason}",
                                f"{sampled} device bytes read"))
                continue
            remaining = list(active)
            coverage_compared = max(compared_by_path.values(), default=0)
            unverified_candidates = any(compared_by_path[image.path] == 0
                                        for image, _size in remaining)
            whole_partition = bool(remaining) and all(
                image.size == partition["size"] and
                all(extent.kind != "undefined" for extent in image.extents)
                for image, _size in remaining
            )
            status = ("Unknown: some candidates have no defined bytes to verify" if unverified_candidates else
                      "Verified whole-partition equality" if whole_partition else
                      "Verified image-content match" if remaining else
                      "Unknown: no defined image bytes compared" if not different else "Different")
        else:
            if remaining and any(compared[image.path] == 0 for image, _size in remaining):
                status = "Unknown: some candidates have no defined sampled bytes"
            else:
                status = "Likely match (not fully verified)" if remaining else "Different"
        if remaining and any(any(extent.kind == "undefined" for extent in image.extents)
                             for image, _size in remaining):
            status += "; DONT_CARE regions unverified"
        if any(not image.unchanged() for image, _size in indexed):
            potential = [(_display_path(image.path, directory), size) for image, size in candidates]
            results.append((partition["name"], potential, "Unknown: an image changed during matching",
                            f"{sampled} device bytes read"))
            continue
        # Keep ambiguous candidates; display basename but never use it for ranking.
        results.append((partition["name"], [(_display_path(i.path, directory), s) for i, s in remaining],
                        status, f"{sampled} device bytes read; up to {coverage_compared} defined image bytes compared"))
        report(
            f"Partition {partition_number}/{len(partition_list)} complete · {status}",
            partition_number / partition_count,
        )
    if directory:
        root = os.path.realpath(directory)
        current = {os.path.realpath(path) for path in _image_files(root)}
        indexed_paths = {image.path for image, _size in indexed}
        if current != indexed_paths:
            results = [
                (partition, matches, "Unknown: image directory changed during matching", coverage)
                for partition, matches, _status, coverage in results
            ]
    return results


def _display_path(path, directory):
    """Path is returned only for UI display; it never participates in matching."""
    return os.path.relpath(path, os.path.realpath(directory)) if directory else os.path.basename(path)


def _distributed_defined_offsets(image):
    """Return up to four distinct 4 KiB windows distributed over defined bytes."""
    intervals = []
    for extent in image.extents:
        if extent.kind == "undefined" or extent.length <= 0:
            continue
        first = extent.start // FINE
        last = (extent.start + extent.length - 1) // FINE
        intervals.append((first, last + 1))
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    total = sum(end - start for start, end in merged)
    count = min(4, total)
    if not count:
        return ()
    ranks = [0] if count == 1 else [i * (total - 1) // (count - 1) for i in range(count)]
    offsets = []
    for rank in ranks:
        for start, end in merged:
            width = end - start
            if rank < width:
                offsets.append((start + rank) * FINE)
                break
            rank -= width
    return tuple(offsets)


def make_da_reader(daloader, partition_geometry, pagesize, observer=None):
    """Adapt authenticated DA random-access reads to the matcher's byte seam."""
    alignment = max(1, int(pagesize or 512))

    def read_at(partition, offset, length):
        geometry = partition_geometry.get(partition["name"])
        if not geometry or offset < 0 or length < 0 or offset + length > geometry["size"]:
            return None
        aligned_offset = offset - offset % alignment
        leading = offset - aligned_offset
        aligned_length = ((leading + length + alignment - 1) // alignment) * alignment
        if aligned_offset + aligned_length > geometry["size"]:
            return None
        addr = geometry["start"] + aligned_offset
        started = time.monotonic()
        data = None
        read_error = None
        try:
            data = daloader.readflash(
                addr=addr, length=aligned_length,
                filename="", parttype=geometry["parttype"], display=False,
            )
        except SystemExit as error:
            # Some MTK DA implementations call sys.exit(1) when readback is
            # unsupported/rejected. Contain that backend behavior in this
            # worker and surface it as an Unknown comparison, not app failure.
            read_error = f"DA backend aborted the read (exit {error.code})"
        except Exception as error:
            read_error = f"{type(error).__name__}: {error}"
        finally:
            if observer:
                observer(offset, length, addr, aligned_length, geometry["parttype"],
                         time.monotonic() - started,
                         len(data) if isinstance(data, (bytes, bytearray)) else 0)
        read_at.last_error = read_error
        if not isinstance(data, (bytes, bytearray)) or len(data) != aligned_length:
            if read_error is None:
                read_at.last_error = (
                    f"DA returned {len(data) if isinstance(data, (bytes, bytearray)) else 0} "
                    f"bytes; expected {aligned_length}"
                )
            return None
        return bytes(data[leading:leading + length])

    return read_at


def diagnose_da_reads(image_path, daloader, partition_geometry, partition_name,
                      pagesize, cancel=None):
    """Perform explicit, read-only random-access probes against a known image.

    The diagnostic records each DA request and instruments the active DA's
    ``usbread`` return path when available. It never reconnects or writes.
    """
    started = time.monotonic()
    image = Image.open(image_path)
    geometry = partition_geometry.get(partition_name)
    observations, offsets, addresses, logical_requests, latencies = [], [], [], [], []
    mtk_session = getattr(daloader, "mtk", None)
    session_before = (id(daloader), id(getattr(daloader, "da", None)),
                      id(mtk_session), id(getattr(mtk_session, "port", None)))
    da = getattr(daloader, "da", None)
    usb_method = getattr(da, "usbread", None)
    usb_calls = [0]
    usb_bytes = [0]
    original_instance_method = getattr(da, "__dict__", {}).get("usbread") if da else None
    instrumented = False
    error = None
    matches = 0
    content_different = False

    def on_read(logical_offset, request_length, addr, length, region, latency, returned):
        observations.append((logical_offset, request_length, addr, length, region, latency, returned))
        addresses.append(addr)
        logical_requests.append(logical_offset)
        latencies.append(latency)

    try:
        if not geometry:
            raise ValueError("selected partition has no known GPT geometry")
        if image.size > geometry["size"]:
            raise ValueError("known image is larger than the selected partition")
        if not image.unchanged():
            raise RuntimeError("local image changed before diagnostic")
        if callable(usb_method):
            def counted_usbread(*args, **kwargs):
                result = usb_method(*args, **kwargs)
                usb_calls[0] += 1
                if isinstance(result, (bytes, bytearray, memoryview)):
                    usb_bytes[0] += len(result)
                return result
            try:
                setattr(da, "usbread", counted_usbread)
                instrumented = True
            except (AttributeError, TypeError):
                pass

        reader = make_da_reader(daloader, partition_geometry, pagesize, on_read)
        offsets = list(_distributed_defined_offsets(image))
        for offset in offsets:
            if cancel and cancel.is_set():
                raise InterruptedError("DA diagnostic cancelled")
            width = min(FINE, image.size - offset)
            defined = list(image.defined_slices(offset, width))
            if not defined:
                continue
            # An intentionally unaligned start exercises DA alignment and trim.
            run_start, run_data = defined[0]
            intra = 37 if len(run_data) > 512 else 0
            logical_start = run_start + intra
            expected = run_data[intra:]
            actual = reader({"name": partition_name}, logical_start, len(expected))
            if actual is None or len(actual) != len(expected):
                error = f"short or failed DA read at logical offset {logical_start}"
                continue
            if actual == expected:
                matches += 1
            else:
                content_different = True
        if not offsets:
            raise ValueError("image has no defined bytes to probe")
        if not image.unchanged():
            raise RuntimeError("local image changed during diagnostic")
    except InterruptedError:
        error = "diagnostic cancelled"
    except Exception as exc:
        error = str(exc)
    finally:
        if instrumented:
            if original_instance_method is None:
                try:
                    delattr(da, "usbread")
                except AttributeError:
                    pass
            else:
                setattr(da, "usbread", original_instance_method)

    mtk_after = getattr(daloader, "mtk", None)
    session_after = (id(daloader), id(getattr(daloader, "da", None)),
                     id(mtk_after), id(getattr(mtk_after, "port", None)))
    session_stable = session_before == session_after
    alignment = max(1, int(pagesize or 512))
    alignment_ok = all(addr % alignment == 0 and length % alignment == 0
                        for _offset, _request, addr, length, _region, _latency, _returned in observations)
    exact_offsets_ok = bool(geometry) and all(
        addr == geometry["start"] + (offset - offset % alignment) and
        length == ((offset % alignment + requested + alignment - 1) // alignment) * alignment
        for offset, requested, addr, length, _region, _latency, _returned in observations
    )
    storage_region_ok = bool(geometry) and all(
        region == geometry["parttype"]
        for _offset, _req, _addr, _len, region, _latency, _ret in observations)
    requests = len(observations)
    bytes_requested = sum(length for _offset, _req, _addr, length, _region, _latency, _ret in observations)
    if (error or requests != len(offsets) or not session_stable or not alignment_ok
            or not exact_offsets_ok or not storage_region_ok):
        status = "UNKNOWN"
    elif content_different:
        status = "DIFFERENT"
    elif matches == requests:
        status = "SAMPLED MATCH (not full verification)"
    else:
        status = "UNKNOWN"
    return DaDiagnosticReport(
        status=status, partition=partition_name, image=os.path.basename(image_path),
        checks=requests, matching_checks=matches, da_requests=requests,
        da_bytes_requested=bytes_requested,
        usb_read_calls=usb_calls[0] if instrumented else None,
        usb_bytes_received=usb_bytes[0] if instrumented else None,
        elapsed_seconds=time.monotonic() - started, read_latencies=tuple(latencies),
        logical_offsets=tuple(logical_requests), device_addresses=tuple(addresses),
        exact_offsets_ok=exact_offsets_ok, alignment_ok=alignment_ok,
        storage_region_ok=storage_region_ok,
        session_stable=session_stable, error=error,
    )
