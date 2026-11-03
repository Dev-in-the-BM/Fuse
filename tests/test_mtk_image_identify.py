import os
import sqlite3
import struct
import threading
import tempfile
import unittest
from pathlib import Path

from mtk_image_identify import (COARSE, Image, ImageIndex, diagnose_da_reads,
                                identify, make_da_reader)


def sparse_file(path, chunks, block=4096):
    header = struct.pack("<IHHHHIIII", 0xED26FF3A, 1, 0, 28, 12, block,
                         sum(item[1] for item in chunks), len(chunks), 0)
    body = bytearray(header)
    for kind, blocks, content in chunks:
        total = 12 + len(content)
        body.extend(struct.pack("<HHII", kind, 0, blocks, total))
        body.extend(content)
    Path(path).write_bytes(body)


class FirmwareIdentifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.index = ImageIndex(self.root / "cache.sqlite3")

    def tearDown(self):
        self.index.db.close()
        self.temp.cleanup()

    def test_has_indexed_images_only_accepts_complete_rows_inside_folder(self):
        folder = self.root / "firmware"
        folder.mkdir()
        image = folder / "boot.img"
        image.write_bytes(b"firmware")
        self.index.db.execute(
            "INSERT INTO images VALUES(?,?,?,?,?,?,?,?)",
            (str(image), image.name, 8, 8, 1, 1, 1, 1),
        )
        self.index.db.commit()
        self.assertTrue(self.index.has_indexed_images(folder))
        self.assertFalse(self.index.has_indexed_images(self.root / "other"))
        self.index.db.execute("UPDATE images SET complete=0")
        self.index.db.commit()
        self.assertFalse(self.index.has_indexed_images(folder))

    def test_raw_and_sparse_defined_content(self):
        raw = self.root / "raw.img"
        raw.write_bytes(b"abc")
        self.assertEqual(Image.open(raw).read(0, 3), b"abc")
        sparse = self.root / "sparse.img"
        sparse_file(sparse, [(0xCAC1, 1, b"A" * 4096),
                             (0xCAC2, 1, b"xyzw"), (0xCAC3, 1, b"")])
        image = Image.open(sparse)
        self.assertEqual(image.size, 12288)
        self.assertEqual(image.read(0, 4), b"AAAA")
        self.assertEqual(image.read(4096, 8), b"xyzwxyzw")
        self.assertIsNone(image.read(8192, 4))
        self.assertEqual(list(image.defined_slices(8190, 8)), [(8190, b"zw")])

    def test_recursive_directory_discovery_cache_and_removal(self):
        nested = self.root / "roms" / "release" / "deeper"
        nested.mkdir(parents=True)
        image_path = nested / "partition.img"
        image_path.write_bytes(b"nested image")
        self.assertEqual(self.index.index_directory(self.root), 1)
        indexed = self.index.images(self.root)
        self.assertEqual([(image.read(0, image.size), size) for image, size in indexed],
                         [(b"nested image", len(b"nested image"))])
        # Unchanged recursive scans use cached metadata, while new descendants appear.
        self.assertEqual(self.index.index_directory(self.root), 1)
        extra = self.root / "another" / "level" / "boot.bin"
        extra.parent.mkdir(parents=True)
        extra.write_bytes(b"boot")
        self.assertEqual(self.index.index_directory(self.root), 2)
        self.assertEqual(len(self.index.images(self.root)), 2)
        image_path.unlink()
        self.assertEqual(self.index.index_directory(self.root), 1)
        self.assertEqual([image.path for image, _ in self.index.images(self.root)],
                         [str(extra.resolve())])

    def test_unchanged_recursive_scan_does_not_rebuild_refinement_bookkeeping(self):
        (self.root / "cached.img").write_bytes(b"cached content")
        self.index.index_directory(self.root)
        calls = []
        original = self.index.refine_directory
        self.index.refine_directory = lambda *args, **kwargs: calls.append(args)
        try:
            self.index.index_directory(self.root)
        finally:
            self.index.refine_directory = original
        self.assertEqual(calls, [])

    def test_recursive_index_reports_discovery_and_byte_progress(self):
        image_path = self.root / "nested" / "deep" / "boot.img"
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(b"progress-data")
        discovery, byte_updates = [], []
        count = self.index.index_directory(
            self.root,
            discovery_progress=lambda folder, found, visited: discovery.append(
                (Path(folder), found, visited)),
            byte_progress=lambda path, number, image_count, done, total: byte_updates.append(
                (Path(path), number, image_count, done, total)),
        )
        self.assertEqual(count, 1)
        self.assertTrue(any(folder == image_path.parent for folder, _, _ in discovery))
        self.assertIn((image_path.resolve(), 1, 1, len(b"progress-data"),
                       len(b"progress-data")), byte_updates)

    def test_fingerprint_refinement_reports_measurable_progress(self):
        left, right = self.root / "one.img", self.root / "two.img"
        left.write_bytes(b"A" * 8192)
        right.write_bytes(b"A" * 4096 + b"B" * 4096)
        phases = []
        self.index.index_directory(self.root, progress=lambda done, total, message:
                                    phases.append((done, total, message)))
        self.assertTrue(any("Comparing coarse fingerprints" in message
                            for _done, _total, message in phases))
        refinement = [(done, total, message) for done, total, message in phases
                      if message.startswith(("Refining", "Fingerprinting"))]
        self.assertTrue(refinement)
        self.assertTrue(any(total > 0 and done > 0 for done, total, _ in refinement))
        self.assertTrue(any("Fingerprint refinement complete" in message
                            for _done, _total, message in phases))

    def test_tiny_content_difference_identifies_without_filename_heuristics(self):
        a, b = self.root / "z.img", self.root / "a.img"
        first = bytearray(b"q" * (1024 * 1024))
        second = bytearray(first)
        second[700_123] = ord("r")
        a.write_bytes(first)
        b.write_bytes(second)
        self.index.index_directory(self.root)
        device = bytes(second)
        result = identify(self.index, [{"name": "system_a", "size": len(device)}],
                          lambda _p, off, length: device[off:off + length],
                          directory=self.root)
        self.assertEqual(result[0][1], [("a.img", len(device))])
        self.assertIn("Likely", result[0][2])

    def test_ambiguous_matches_and_prefix(self):
        (self.root / "one.bin").write_bytes(b"hello")
        (self.root / "two.img").write_bytes(b"hello")
        self.index.index_directory(self.root)
        completed = []
        result = identify(self.index, [{"name": "boot", "size": 32}],
                          lambda _p, off, length: b"hello"[off:off + length],
                          directory=self.root, on_partition_result=completed.append)
        self.assertEqual(len(result[0][1]), 2)
        self.assertEqual(completed, result)

    def test_partition_result_callback_runs_before_next_partition_scan(self):
        (self.root / "boot.img").write_bytes(b"hello")
        self.index.index_directory(self.root)
        completed = []

        def read_at(partition, offset, length):
            if partition["name"] == "system":
                self.assertEqual([result[0] for result in completed], ["boot"])
            return b"hello"[offset:offset + length]

        identify(
            self.index,
            [{"name": "boot", "size": 2}, {"name": "system", "size": 32}],
            read_at,
            directory=self.root,
            on_partition_result=completed.append,
        )
        self.assertEqual([result[0] for result in completed], ["boot", "system"])

    def test_cache_metadata_invalidation_and_force(self):
        path = self.root / "cache.bin"
        path.write_bytes(b"before")
        self.assertTrue(self.index.index_file(path))
        self.assertFalse(self.index.index_file(path))
        path.write_bytes(b"after!")
        os.utime(path, ns=(os.stat(path).st_atime_ns, os.stat(path).st_mtime_ns + 1_000_000))
        self.assertEqual(self.index.images()[0][0].read(0, 6), b"after!")
        self.assertTrue(self.index.index_file(path, force=True))

    def test_legacy_cache_schema_is_migrated(self):
        legacy_path = self.root / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as db:
            db.execute("CREATE TABLE images(path TEXT PRIMARY KEY, display TEXT NOT NULL, size INTEGER NOT NULL, logical_size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL, complete INTEGER NOT NULL)")
            db.execute("CREATE TABLE chunks(path TEXT NOT NULL, level INTEGER NOT NULL, offset INTEGER NOT NULL, length INTEGER NOT NULL, digest BLOB NOT NULL, PRIMARY KEY(path,level,offset))")
        legacy = ImageIndex(legacy_path)
        primary_key = tuple(row[1] for row in sorted(
            (row for row in legacy.db.execute("PRAGMA table_info(chunks)") if row[5]),
            key=lambda row: row[5]))
        self.assertEqual(primary_key, ("path", "level", "offset", "length"))
        legacy.db.close()

    def test_cancelled_index_is_not_committed_complete(self):
        path = self.root / "cancelled.img"
        path.write_bytes(b"x" * (3 * 1024 * 1024))
        cancel = threading.Event()
        def progress(done, _total):
            if done >= 1024 * 1024:
                cancel.set()
        with self.assertRaises(InterruptedError):
            self.index.index_file(path, cancel=cancel, progress=progress)
        self.assertIsNone(self.index.db.execute(
            "SELECT 1 FROM images WHERE path=? AND complete=1", (str(path.resolve()),)
        ).fetchone())

    def test_file_change_during_index_does_not_publish_partial_index(self):
        path = self.root / "racing.img"
        path.write_bytes(b"a" * (2 * 1024 * 1024))
        changed = False
        def mutate_after_first_coarse_chunk(done, _total):
            nonlocal changed
            if done >= 1024 * 1024 and not changed:
                changed = True
                with path.open("r+b") as stream:
                    stream.seek(1500 * 1024)
                    stream.write(b"z")
        with self.assertRaisesRegex(RuntimeError, "changed while"):
            self.index.index_file(path, progress=mutate_after_first_coarse_chunk)
        self.assertIsNone(self.index.db.execute(
            "SELECT 1 FROM images WHERE path=?", (str(path.resolve()),)
        ).fetchone())

    def test_read_failure_is_unknown(self):
        (self.root / "boot.img").write_bytes(b"firmware")
        self.index.index_directory(self.root)
        result = identify(self.index, [{"name": "boot", "size": 64}],
                          lambda *_: None, directory=self.root)
        self.assertIn("Unknown", result[0][2])
        self.assertEqual(result[0][1], [("boot.img", 8)])

    def test_differentiating_offset_is_read_before_distributed_samples(self):
        data_a = bytearray(b"x" * (4 * 1024 * 1024))
        data_b = bytearray(data_a)
        data_b[123] = ord("y")
        (self.root / "candidate-a.img").write_bytes(data_a)
        (self.root / "candidate-b.img").write_bytes(data_b)
        self.index.index_directory(self.root)
        reads = []
        device = bytes(data_b)
        result = identify(
            self.index, [{"name": "system", "size": len(device)}],
            lambda _p, offset, length: reads.append((offset, length)) or device[offset:offset + length],
            directory=self.root,
        )
        self.assertEqual(reads[0], (0, 4096))
        self.assertEqual(len(reads), 4)
        self.assertEqual({offset for offset, _length in reads},
                         {0, 1_396_736, 2_793_472, 4_190_208})
        self.assertEqual(result[0][1], [("candidate-b.img", len(device))])

    def test_fine_offset_query_is_bounded_to_strong_regions(self):
        one = bytearray(b"A" * (1024 * 1024))
        two = bytearray(one)
        for offset in range(0, len(two), 4096):
            two[offset] = ord("B")
        (self.root / "one.img").write_bytes(one)
        (self.root / "two.img").write_bytes(two)
        self.index.index_directory(self.root)
        candidates = self.index.images(self.root)
        offsets = self.index.distinguishing_offsets(candidates)
        self.assertLessEqual(len(offsets), 24)
        self.assertTrue(offsets)

    def test_identical_partition_candidate_sets_reuse_sample_plan(self):
        content = b"shared image content" * 500
        (self.root / "candidate.img").write_bytes(content)
        self.index.index_directory(self.root)
        calls = []
        original = self.index.distinguishing_offsets
        self.index.distinguishing_offsets = lambda candidates, **kwargs: (
            calls.append(len(candidates)) or original(candidates, **kwargs)
        )
        try:
            identify(
                self.index,
                [{"name": "boot_a", "size": len(content)},
                 {"name": "boot_b", "size": len(content)}],
                lambda _p, offset, length: content[offset:offset + length],
                directory=self.root,
            )
        finally:
            self.index.distinguishing_offsets = original
        self.assertEqual(calls, [1])

    def test_full_verification_skips_quick_offset_plan(self):
        content = b"verify without offset planning" * 100
        (self.root / "candidate.img").write_bytes(content)
        self.index.index_directory(self.root)
        original = self.index.distinguishing_offsets
        self.index.distinguishing_offsets = lambda *_args, **_kwargs: self.fail(
            "full verification does not need quick fingerprint samples"
        )
        try:
            result = identify(
                self.index, [{"name": "boot", "size": len(content)}],
                lambda _p, offset, length: content[offset:offset + length],
                full=True, directory=self.root,
            )
        finally:
            self.index.distinguishing_offsets = original
        self.assertIn("Verified whole-partition equality", result[0][2])

    def test_ambiguous_distributed_sampling_starts_at_about_16k(self):
        content = b"a" * (1024 * 1024)
        (self.root / "copy-one.bin").write_bytes(content)
        (self.root / "copy-two.bin").write_bytes(content)
        self.index.index_directory(self.root)
        reads = []
        result = identify(
            self.index, [{"name": "boot", "size": len(content)}],
            lambda _p, offset, length: reads.append((offset, length)) or content[offset:offset + length],
            directory=self.root,
        )
        self.assertEqual(len(result[0][1]), 2)
        self.assertEqual(sum(length for _offset, length in reads), 16 * 1024)
        self.assertEqual(len(reads), 4)

    def test_tiny_image_uses_all_available_defined_bytes_once(self):
        tiny = b"tiny-img"
        (self.root / "tiny.img").write_bytes(tiny)
        self.index.index_directory(self.root)
        reads = []
        result = identify(
            self.index, [{"name": "boot", "size": 4096}],
            lambda _p, offset, length: reads.append((offset, length)) or tiny[offset:offset + length],
            directory=self.root,
        )
        self.assertEqual(reads, [(0, len(tiny))])
        self.assertIn("Likely match", result[0][2])

    def test_sparse_quick_samples_four_distributed_defined_windows(self):
        pieces = [(0xCAC1, 1, b"a" * 4096), (0xCAC3, 2, b""),
                  (0xCAC1, 1, b"b" * 4096), (0xCAC3, 1, b""),
                  (0xCAC1, 1, b"c" * 4096), (0xCAC3, 1, b""),
                  (0xCAC1, 1, b"d" * 4096)]
        sparse_file(self.root / "sparse.img", pieces)
        self.index.index_directory(self.root)
        defined = b"a" * 4096 + b"b" * 4096 + b"c" * 4096 + b"d" * 4096
        def read_device(_partition, offset, length):
            if offset < 4096:
                return defined[:length]
            if 12288 <= offset < 16384:
                return defined[4096:4096 + length]
            if 20480 <= offset < 24576:
                return defined[8192:8192 + length]
            if 28672 <= offset < 32768:
                return defined[12288:12288 + length]
            return b"?" * length
        calls = []
        def record(*args):
            calls.append((args[1], args[2]))
            return read_device(*args)
        result = identify(self.index, [{"name": "system", "size": 40960}],
                          record, directory=self.root)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(length == 4096 for _offset, length in calls))
        self.assertIn("Likely match", result[0][2])

    def test_overlapping_sparse_difference_and_distributed_reads_reuse_bytes(self):
        first = bytearray(b"A" * 8200)
        second = bytearray(first)
        second[23] = ord("B")
        sparse_file(self.root / "first.img", [(0xCAC3, 1, b""),
                                                (0xCAC1, 82, bytes(first))], block=100)
        sparse_file(self.root / "second.img", [(0xCAC3, 1, b""),
                                                 (0xCAC1, 82, bytes(second))], block=100)
        self.index.index_directory(self.root)
        device = b"?" * 100 + bytes(second)
        reads = []
        def read_device(_partition, offset, length):
            reads.append((offset, length))
            return device[offset:offset + length]
        result = identify(self.index, [{"name": "system", "size": 9000}],
                          read_device, directory=self.root)
        self.assertEqual(reads[0], (100, 4096))
        self.assertIn((0, 100), reads)  # only the uncovered edge of the overlapping sample
        self.assertLess(sum(length for _offset, length in reads), 3 * 4096)
        self.assertEqual(result[0][1], [("second.img", 8300)])

    def test_full_verification_reuses_reads_across_identical_candidates(self):
        content = b"full-verification" * 1000
        (self.root / "copy-one.img").write_bytes(content)
        (self.root / "copy-two.img").write_bytes(content)
        self.index.index_directory(self.root)
        reads = []
        result = identify(
            self.index, [{"name": "boot", "size": len(content) + 4096}],
            lambda _p, offset, length: reads.append((offset, length)) or content[offset:offset + length],
            full=True, directory=self.root,
        )
        self.assertEqual(len(result[0][1]), 2)
        self.assertEqual(sum(length for _offset, length in reads), len(content))
        self.assertEqual(len(reads), (len(content) + COARSE - 1) // COARSE)

    def test_all_dont_care_is_unknown_not_a_match(self):
        path = self.root / "hole.img"
        sparse_file(path, [(0xCAC3, 1, b"")])
        self.index.index_directory(self.root)
        result = identify(self.index, [{"name": "vendor", "size": 8192}],
                          lambda *_: b"\0" * 4096, directory=self.root)
        self.assertIn("Unknown", result[0][2])
        self.assertEqual(result[0][1], [("hole.img", 4096)])

    def test_sparse_undefined_candidate_is_not_silently_eliminated_or_verified(self):
        sparse_file(self.root / "defined.img", [(0xCAC1, 1, b"D" * 4096)])
        sparse_file(self.root / "unspecified.img", [(0xCAC3, 1, b"")])
        self.index.index_directory(self.root)
        for full in (False, True):
            result = identify(
                self.index, [{"name": "system", "size": 8192}],
                lambda _p, off, length: (b"D" * 4096)[off:off + length],
                full=full, directory=self.root,
            )
            self.assertEqual(len(result[0][1]), 2)
            self.assertIn("Unknown", result[0][2])

    def test_readback_exception_is_unknown(self):
        (self.root / "boot.img").write_bytes(b"firmware")
        self.index.index_directory(self.root)
        def disconnected(*_args):
            raise TimeoutError("mock DA timeout")
        result = identify(self.index, [{"name": "boot", "size": 64}],
                          disconnected, directory=self.root)
        self.assertIn("Unknown", result[0][2])
        self.assertEqual(result[0][1], [("boot.img", 8)])

    def test_mocked_da_reader_aligns_and_respects_partition_bounds(self):
        class MockDa:
            def __init__(self):
                self.calls = []
            def readflash(self, **kwargs):
                self.calls.append(kwargs)
                return bytes(i % 256 for i in range(kwargs["length"]))
        da = MockDa()
        geometry = {"boot_a": {"name": "boot_a", "start": 0x8000,
                               "size": 8192, "parttype": "user"}}
        read_at = make_da_reader(da, geometry, 512)
        data = read_at({"name": "boot_a"}, 5, 4096)
        self.assertEqual(len(data), 4096)
        self.assertEqual(data, bytes(i % 256 for i in range(5, 4101)))
        self.assertEqual(da.calls, [{"addr": 0x8000, "length": 4608,
                                     "filename": "", "parttype": "user", "display": False}])
        self.assertIsNone(read_at({"name": "boot_a"}, 8191, 2))
        self.assertEqual(len(da.calls), 1)

    def test_da_reader_contains_backend_system_exit_as_unknown_read(self):
        class MockDa:
            def readflash(self, **_kwargs):
                raise SystemExit(1)
        image_path = self.root / "read-failure.img"
        image_path.write_bytes(b"contents")
        self.index.index_directory(self.root)
        geometry = {"boot": {"name": "boot", "start": 0x1000,
                             "size": 8192, "parttype": "user"}}
        read_at = make_da_reader(MockDa(), geometry, 512)
        result = identify(
            self.index, [geometry["boot"]], read_at, directory=self.root,
        )
        self.assertIn("Unknown", result[0][2])
        self.assertIn("DA backend aborted the read (exit 1)", result[0][2])

    def test_read_only_da_diagnostic_reports_alignment_region_and_usb_telemetry(self):
        image_path = self.root / "known.img"
        content = bytes((i * 29) % 256 for i in range(64 * 1024))
        image_path.write_bytes(content)
        geometry = {"boot_a": {"name": "boot_a", "start": 0x20000,
                               "size": len(content), "parttype": "user"}}

        class MockDa:
            def __init__(self):
                self.read_count = 0
            def usbread(self, length):
                self.read_count += 1
                return b"0" * length

        class MockLoader:
            def __init__(self):
                self.da = MockDa()
                self.mtk = object()
                self.requests = []
            def readflash(self, **kwargs):
                self.requests.append(kwargs)
                self.da.usbread(kwargs["length"])
                start = kwargs["addr"] - geometry["boot_a"]["start"]
                return content[start:start + kwargs["length"]]

        loader = MockLoader()
        report = diagnose_da_reads(image_path, loader, geometry, "boot_a", 512)
        self.assertIn("SAMPLED MATCH", report.status)
        self.assertEqual(report.checks, 4)
        self.assertEqual(report.matching_checks, 4)
        self.assertEqual(report.da_requests, 4)
        self.assertEqual(report.da_bytes_requested, 4 * 4096)
        self.assertEqual(report.usb_read_calls, 4)
        self.assertEqual(report.usb_bytes_received, 4 * 4096)
        self.assertTrue(report.alignment_ok)
        self.assertTrue(report.storage_region_ok)
        self.assertTrue(report.session_stable)
        self.assertEqual({request["parttype"] for request in loader.requests}, {"user"})
        self.assertTrue(all(request["addr"] % 512 == 0 and request["length"] % 512 == 0
                            for request in loader.requests))
        self.assertEqual(report.device_addresses,
                         tuple(geometry["boot_a"]["start"] + offset - offset % 512
                               for offset in report.logical_offsets))
        self.assertTrue(report.exact_offsets_ok)
        self.assertEqual(len(report.read_latencies), 4)
        self.assertNotIn("usbread", loader.da.__dict__)
        self.assertTrue(callable(loader.da.usbread))

    def test_diagnostic_reports_known_image_mismatch_without_writes(self):
        image_path = self.root / "known.bin"
        image_path.write_bytes(b"A" * 8192)
        geometry = {"boot": {"name": "boot", "start": 4096,
                             "size": 8192, "parttype": "user"}}
        class MockDa:
            def usbread(self, length):
                return b"X" * length
        class MockLoader:
            da = MockDa()
            mtk = object()
            def readflash(self, **kwargs):
                return b"B" * kwargs["length"]
        report = diagnose_da_reads(image_path, MockLoader(), geometry, "boot", 512)
        self.assertEqual(report.status, "DIFFERENT")
        self.assertEqual(report.checks, 2)
        self.assertEqual(report.matching_checks, 0)

    def test_image_mutation_during_matching_downgrades_to_unknown(self):
        path = self.root / "changing.bin"
        path.write_bytes(b"original-data" * 1000)
        self.index.index_directory(self.root)
        content = path.read_bytes()
        mutated = False
        def mocked_da(_part, offset, length):
            nonlocal mutated
            if not mutated:
                mutated = True
                with path.open("r+b") as stream:
                    stream.seek(0)
                    stream.write(b"X")
            return content[offset:offset + length]
        result = identify(self.index, [{"name": "boot", "size": 20000}],
                          mocked_da, directory=self.root)
        self.assertIn("Unknown", result[0][2])

    def test_directory_change_during_scan_is_unknown_and_next_scan_discovers_new_file(self):
        first = self.root / "first.img"
        second = self.root / "second.bin"
        content = b"directory-race" * 600
        first.write_bytes(content)
        self.index.index_directory(self.root)
        added = False
        def mocked_da(_part, offset, length):
            nonlocal added
            if not added:
                added = True
                second.write_bytes(content)
            return content[offset:offset + length]
        partition = [{"name": "system", "size": 16384}]
        result = identify(self.index, partition, mocked_da, directory=self.root)
        self.assertIn("Unknown", result[0][2])
        next_result = identify(self.index, partition,
                               lambda _p, off, length: content[off:off + length],
                               directory=self.root)
        self.assertEqual(len(next_result[0][1]), 2)

    def test_full_verification_compares_defined_prefix_only(self):
        path = self.root / "prefix.img"
        path.write_bytes(b"prefix-content")
        self.index.index_directory(self.root)
        content = path.read_bytes()
        result = identify(self.index, [{"name": "vendor_b", "size": 4096}],
                          lambda _p, off, length: content[off:off + length],
                          full=True, directory=self.root)
        self.assertIn("Verified image-content", result[0][2])
        self.assertEqual(result[0][1], [("prefix.img", len(content))])

    def test_matching_progress_is_emitted_before_and_after_device_reads(self):
        image_path = self.root / "boot.img"
        image_path.write_bytes(b"firmware-content" * 1024)
        self.index.index_directory(self.root)
        events = []
        content = image_path.read_bytes()

        def read_at(_partition, offset, length):
            events.append(("read", offset, length))
            return content[offset:offset + length]

        results = identify(
            self.index, [{"name": "boot", "size": len(content)}], read_at,
            directory=self.root,
            progress=lambda partition, count, message, fraction: events.append(
                ("progress", partition, count, message, fraction)
            ),
        )
        read_index = next(i for i, event in enumerate(events) if event[0] == "read")
        self.assertTrue(any(event[0] == "progress" and "at image offset" in event[3]
                            for event in events[:read_index]))
        self.assertTrue(any(event[0] == "progress" and "complete" in event[3]
                            for event in events[read_index + 1:]))
        self.assertTrue(results[0][2].startswith("Likely match"))

    def test_full_exact_partition_match_is_classified_separately(self):
        path = self.root / "exact.img"
        content = b"whole-partition" * 300
        path.write_bytes(content)
        self.index.index_directory(self.root)
        result = identify(self.index, [{"name": "boot", "size": len(content)}],
                          lambda _p, off, length: content[off:off + length],
                          full=True, directory=self.root)
        self.assertIn("Verified whole-partition equality", result[0][2])

    def test_full_sparse_verification_skips_dont_care_usb_ranges(self):
        sparse = self.root / "sparse.img"
        sparse_file(sparse, [(0xCAC1, 1, b"A" * 4096),
                             (0xCAC2, 1, b"xyzw"), (0xCAC3, 1, b"")])
        self.index.index_directory(self.root)
        reads = []
        def mocked_da(_part, offset, length):
            reads.append((offset, length))
            expanded = b"A" * 4096 + b"xyzw" * 1024
            return expanded[offset:offset + length]
        result = identify(self.index, [{"name": "system", "size": 16384}],
                          mocked_da, full=True, directory=self.root)
        self.assertIn("Verified image-content", result[0][2])
        self.assertEqual(reads, [(0, 8192)])


if __name__ == "__main__":
    unittest.main()
