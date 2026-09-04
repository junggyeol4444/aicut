"""Fetching a static ffmpeg (20.1).

The publishers' hosts are blocked in the environment this was written in, so
the real download is not confirmed here. What IS confirmed is everything the
program does with the bytes once they arrive - verification, unpacking, and
the refusals - by serving the archive from a local HTTP server. That is the
part with the security consequences: an unverified ffmpeg would run whatever
the network handed back, and an archive that writes outside its folder is an
attack rather than a bug.
"""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import tempfile
import threading
import unittest
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aicut.media.ffmpeg_fetch import (
    Build,
    FetchRefused,
    bundled_ffmpeg,
    fetch,
    install_dir,
    platform_build,
    use_bundled,
)


def _tar_archive(target: Path, names=("ffmpeg", "ffprobe"), *, extra=None) -> Path:
    """A tar shaped like the real static builds: binaries inside a folder."""
    with tarfile.open(target, "w:gz") as tf:
        for name in names:
            payload = b"#!/bin/sh\necho fake ffmpeg\n"
            info = tarfile.TarInfo(f"ffmpeg-7.1-static/{name}")
            info.size = len(payload)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(payload))
        if extra:
            info = tarfile.TarInfo(extra)
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
    return target


class _Server:
    """Serve one directory on localhost for the duration of a test."""

    def __init__(self, directory: Path):
        handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FetchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self._tmp.name)
        self.served = self.dir / "served"
        self.served.mkdir()
        self.workspace = self.dir / "ws"
        self.server = _Server(self.served)

    def tearDown(self):
        self.server.close()
        self._tmp.cleanup()

    def _build(self, path: Path, *, digest: str | None = None, members=("ffmpeg",)) -> Build:
        real = hashlib.sha256(path.read_bytes()).hexdigest()
        return Build(url=self.server.url(path.name), sha256=digest if digest is not None else real,
                     members=members)

    def test_a_verified_archive_installs_both_binaries(self):
        archive = _tar_archive(self.served / "ffmpeg.tar.gz")
        target = fetch(self.workspace, build=self._build(archive))
        self.assertTrue((target / "ffmpeg").exists())
        self.assertTrue((target / "ffprobe").exists())
        self.assertEqual(target, install_dir(self.workspace))

    def test_the_installed_binary_is_executable(self):
        archive = _tar_archive(self.served / "ffmpeg.tar.gz")
        target = fetch(self.workspace, build=self._build(archive))
        self.assertTrue(os.access(target / "ffmpeg", os.X_OK),
                        "a downloaded ffmpeg that cannot be executed is not installed")

    def test_a_wrong_checksum_installs_nothing(self):
        archive = _tar_archive(self.served / "ffmpeg.tar.gz")
        bad = self._build(archive, digest="0" * 64)
        with self.assertRaises(FetchRefused) as raised:
            fetch(self.workspace, build=bad)
        self.assertIn("checksum", str(raised.exception))
        self.assertIsNone(bundled_ffmpeg(self.workspace), "a rejected download was kept")

    def test_no_recorded_checksum_is_a_refusal_not_a_shrug(self):
        """An unverified ffmpeg runs whatever the network handed back."""
        archive = _tar_archive(self.served / "ffmpeg.tar.gz")
        with self.assertRaises(FetchRefused) as raised:
            fetch(self.workspace, build=self._build(archive, digest=""))
        self.assertIn("verified", str(raised.exception))

    def test_an_archive_that_writes_outside_its_folder_is_refused(self):
        archive = _tar_archive(self.served / "evil.tar.gz", extra="../../escaped.txt")
        with self.assertRaises(FetchRefused) as raised:
            fetch(self.workspace, build=self._build(archive))
        self.assertIn("outside", str(raised.exception))
        self.assertFalse((self.dir.parent / "escaped.txt").exists())

    def test_a_zip_works_too(self):
        path = self.served / "ffmpeg.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("ffmpeg-7.1/bin/ffmpeg.exe", "binary")
            zf.writestr("ffmpeg-7.1/bin/ffprobe.exe", "binary")
        target = fetch(self.workspace, build=self._build(path))
        self.assertTrue((target / "ffmpeg.exe").exists())
        self.assertTrue((target / "ffprobe.exe").exists())

    def test_an_archive_with_no_ffmpeg_in_it_is_refused(self):
        path = self.served / "empty.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("readme.txt", "nothing here")
        with self.assertRaises(FetchRefused) as raised:
            fetch(self.workspace, build=self._build(path))
        self.assertIn("no ffmpeg binary", str(raised.exception))

    def test_an_unreachable_host_says_so_rather_than_half_installing(self):
        build = Build(url="http://127.0.0.1:1/nothing.tar.gz", sha256="0" * 64, members=("ffmpeg",))
        with self.assertRaises(FetchRefused) as raised:
            fetch(self.workspace, build=build, timeout=3)
        self.assertIn("could not download", str(raised.exception))
        self.assertIsNone(bundled_ffmpeg(self.workspace))


class UseBundledTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.workspace = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_system_ffmpeg_is_left_alone(self):
        """A build the user installed is theirs; overriding it silently changes
        behaviour nobody asked to change."""
        from unittest import mock

        with mock.patch("aicut.media.ffmpeg_util.have_ffmpeg", return_value=True):
            self.assertFalse(use_bundled(self.workspace))

    def test_nothing_to_use_when_nothing_was_fetched(self):
        from unittest import mock

        with mock.patch("aicut.media.ffmpeg_util.have_ffmpeg", return_value=False):
            self.assertFalse(use_bundled(self.workspace))


class PlatformBuildTests(unittest.TestCase):
    def test_this_platform_has_a_recorded_build(self):
        build = platform_build()
        self.assertTrue(build.url.startswith("https://"),
                        "an ffmpeg fetched over plain HTTP could be swapped in transit")

    def test_every_recorded_build_uses_https(self):
        from aicut.media.ffmpeg_fetch import BUILDS

        for key, build in BUILDS.items():
            with self.subTest(platform=key):
                self.assertTrue(build.url.startswith("https://"))


if __name__ == "__main__":
    unittest.main()
