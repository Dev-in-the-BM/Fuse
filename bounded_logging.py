"""Keep MTK Client's shared debug log within a configurable disk budget."""

import logging
import os
from pathlib import Path
import tempfile


_SEGMENT_COUNT = 10
_TRIM_MARKER = b"[Fuse] Older log data trimmed to the configured storage limit.\n"
_backend_root = None
_max_total_bytes = 50 * 1024 * 1024
_original_file_handler = None


def configure_backend_file_logging(backend_dir, max_total_bytes):
    """Bound all backend ``log.txt`` handlers to one rotating file budget.

    MTK Client creates a FileHandler in several classes, all targeting log.txt.
    They share one rotating handler here, so rotation stays serialized and a
    record propagated through multiple logger levels is written only once.
    Existing log files are compacted on startup before the handler opens them.
    """
    global _backend_root, _max_total_bytes, _original_file_handler
    if isinstance(max_total_bytes, bool) or not isinstance(max_total_bytes, int):
        raise ValueError("Log budget must be a positive integer number of bytes")
    if max_total_bytes < _SEGMENT_COUNT:
        raise ValueError(f"Log budget must be at least {_SEGMENT_COUNT} bytes")

    _backend_root = Path(backend_dir).expanduser().resolve()
    _max_total_bytes = max_total_bytes
    if _original_file_handler is None:
        _original_file_handler = logging.FileHandler
        logging.FileHandler = _FileHandlerProxy

    path = _backend_root / "logs" / "log.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    return BoundedBackendLogHandler.for_path(path)


def restore_file_handler_for_tests():
    """Restore the standard handler and close cached handlers (test helper)."""
    global _backend_root, _original_file_handler
    if _original_file_handler is not None:
        logging.FileHandler = _original_file_handler
        _original_file_handler = None
    for handler in tuple(BoundedBackendLogHandler._handlers.values()):
        handler.close()
    BoundedBackendLogHandler._handlers.clear()
    _backend_root = None


class BoundedBackendLogHandler(logging.Handler):
    """Shared rotating handler; active and backup files share one hard cap."""

    _handlers = {}

    def __init__(self, log_path, max_total_bytes):
        super().__init__(logging.DEBUG)
        self.baseFilename = os.path.abspath(log_path)
        self.encoding = "utf-8"
        self.terminator = "\n"
        self.segment_bytes = max(1, max_total_bytes // _SEGMENT_COUNT)
        self.backup_count = max(0, max_total_bytes // self.segment_bytes - 1)
        self.max_total_bytes = self.segment_bytes * (self.backup_count + 1)
        self.stream = None

        path = Path(self.baseFilename)
        path.parent.mkdir(parents=True, exist_ok=True)
        _compact_existing_logs(
            path, self.max_total_bytes, self.segment_bytes, self.backup_count
        )
        self.stream = path.open("a", encoding=self.encoding, errors="replace")

    @staticmethod
    def managed_path(filename):
        if _backend_root is None:
            return None
        try:
            path = Path(os.fsdecode(filename)).expanduser()
            if not path.is_absolute():
                path = Path.cwd() / path
            path = path.resolve()
            if path.name != "log.txt":
                return None
            path.relative_to(_backend_root)
        except (OSError, TypeError, ValueError):
            return None
        # MTK Client has both logs/log.txt and log.txt call sites. Keep them in
        # the same managed log so neither path can evade the aggregate limit.
        return _backend_root / "logs" / "log.txt"

    @classmethod
    def for_path(cls, log_path):
        key = os.fspath(log_path)
        handler = cls._handlers.get(key)
        if handler is None:
            handler = cls(log_path, _max_total_bytes)
            cls._handlers[key] = handler
        return handler

    def emit(self, record):
        # The same handler is attached to several MTK Client loggers. A
        # propagated record must only be written once.
        if getattr(record, "_fuse_backend_file_logged", False):
            return
        record._fuse_backend_file_logged = True

        try:
            message = self.format(record)
            encoded = self._encode(message + self.terminator)
            if len(encoded) > self.segment_bytes:
                message = self._truncate_message(message)
                encoded = self._encode(message + self.terminator)

            if self.stream is None or self.stream.closed:
                self.stream = Path(self.baseFilename).open(
                    "a", encoding=self.encoding, errors="replace"
                )
            current_size = os.fstat(self.stream.fileno()).st_size
            if current_size + len(encoded) > self.segment_bytes:
                self._rotate()
            self.stream.write(message + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)

    def _rotate(self):
        self.flush()
        if self.stream is not None:
            self.stream.close()
            self.stream = None

        base = Path(self.baseFilename)
        if self.backup_count:
            oldest = Path(f"{base}.{self.backup_count}")
            oldest.unlink(missing_ok=True)
            for index in range(self.backup_count - 1, 0, -1):
                source = Path(f"{base}.{index}")
                if source.exists():
                    os.replace(source, Path(f"{base}.{index + 1}"))
            if base.exists():
                os.replace(base, Path(f"{base}.1"))
        else:
            base.unlink(missing_ok=True)

        self.stream = base.open("a", encoding=self.encoding, errors="replace")

    def _encode(self, text):
        return text.encode(self.encoding, errors="replace")

    def _truncate_message(self, message):
        marker = "\n[Fuse] Oversized log record truncated to fit this segment."
        terminator_bytes = len(self._encode(self.terminator))
        marker_bytes = self._encode(marker)
        prefix_limit = max(0, self.segment_bytes - terminator_bytes - len(marker_bytes))
        prefix = self._encode(message)[:prefix_limit].decode(
            self.encoding, errors="ignore"
        )
        return prefix + marker

    def flush(self):
        if self.stream is not None and not self.stream.closed:
            self.stream.flush()

    def close(self):
        try:
            self.flush()
            if self.stream is not None and not self.stream.closed:
                self.stream.close()
        finally:
            super().close()


class _FileHandlerProxy(_original_file_handler if _original_file_handler else logging.FileHandler):
    """Route MTK log.txt FileHandlers to the shared bounded implementation."""

    def __new__(cls, filename, *args, **kwargs):
        managed_path = BoundedBackendLogHandler.managed_path(filename)
        if managed_path is not None:
            return BoundedBackendLogHandler.for_path(managed_path)
        return super().__new__(cls)

    def __init__(self, filename, mode="a", encoding=None, delay=False, errors=None):
        # RotatingFileHandler calls logging.FileHandler.__init__ dynamically;
        # keep that path working if another library uses it after Fuse starts.
        _original_file_handler.__init__(
            self, filename, mode=mode, encoding=encoding,
            delay=delay, errors=errors,
        )


def _compact_existing_logs(log_path, max_bytes, segment_bytes, backup_count):
    """Trim prior logs and normalize them into bounded-size rotation segments."""
    existing = _numbered_log_files(log_path)
    if not existing:
        return

    sizes = {path: path.stat().st_size for path in existing}
    total_size = sum(sizes.values())
    needs_normalization = (
        total_size > max_bytes
        or len(existing) > backup_count + 1
        or any(size > segment_bytes for size in sizes.values())
    )
    if not needs_normalization:
        return

    # Active file first, then .1, .2, ... (newest to oldest). Keep the newest
    # bytes across all of them, and reserve room for the trim notice.
    usable_bytes = max(0, max_bytes - len(_TRIM_MARKER))
    selected = []
    remaining = usable_bytes
    for path in existing:
        take = min(sizes[path], remaining)
        if take:
            selected.append((path, sizes[path], take))
            remaining -= take
        if remaining == 0:
            break

    parent = log_path.parent
    staged_paths = []
    combined_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=parent, prefix=".fuse-log-combined-", delete=False
        ) as combined:
            combined_path = Path(combined.name)
            combined.write(_TRIM_MARKER)
            for path, size, take in reversed(selected):
                with path.open("rb") as source:
                    source.seek(size - take)
                    _copy_exactly(source, combined, take)

        combined_size = combined_path.stat().st_size
        segment_count = min(
            backup_count + 1,
            (combined_size + segment_bytes - 1) // segment_bytes,
        )
        for index in range(segment_count):
            end = combined_size - index * segment_bytes
            start = max(0, end - segment_bytes)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=parent, prefix=".fuse-log-segment-", delete=False
            ) as staged:
                staged_path = Path(staged.name)
                staged_paths.append((index, staged_path))
                with combined_path.open("rb") as source:
                    source.seek(start)
                    _copy_exactly(source, staged, end - start)

        # Install backups before the active file. Replacing each staged file
        # atomically avoids deleting the old log set before the compacted copy
        # is ready; clean up any obsolete backup numbers afterward.
        installed = set()
        for index, staged_path in reversed(staged_paths):
            suffix = "" if index == 0 else f".{index}"
            target = Path(f"{log_path}{suffix}")
            os.replace(staged_path, target)
            installed.add(target)
        for path in existing:
            if path not in installed:
                path.unlink(missing_ok=True)
    finally:
        if combined_path is not None:
            combined_path.unlink(missing_ok=True)
        for _index, staged_path in staged_paths:
            staged_path.unlink(missing_ok=True)


def _numbered_log_files(log_path):
    files = []
    if log_path.is_file():
        files.append((0, log_path))
    prefix = log_path.name + "."
    for path in log_path.parent.glob(prefix + "*"):
        suffix = path.name[len(prefix):]
        if path.is_file() and suffix.isdigit():
            files.append((int(suffix), path))
    return [path for _number, path in sorted(files)]


def _copy_exactly(source, destination, count):
    remaining = count
    while remaining:
        chunk = source.read(min(1024 * 1024, remaining))
        if not chunk:
            break
        destination.write(chunk)
        remaining -= len(chunk)
