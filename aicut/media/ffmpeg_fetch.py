"""Fetch a static ffmpeg into the app's own folder (20.1).

Everything this program does to video goes through ffmpeg, and telling someone
who double-clicked an executable to "install ffmpeg and put it on PATH" is
telling them the program does not work. This downloads a static build next to
their projects and uses it from there, without touching the system.

What it deliberately does not do:

* **Guess.** Each platform's build has a named URL and a SHA-256 recorded with
  it. A download whose digest does not match is deleted, not used - an ffmpeg
  fetched over a hijacked connection would be handed every file on the machine.
* **Silently prefer its own copy.** An ffmpeg already on PATH wins; this is for
  the machine that has none.
* **Claim to have been tested end to end here.** The download hosts are blocked
  in the environment this was written in, so the request path is exercised
  against a local server in the tests and the real hosts are not reachable to
  confirm. That is recorded rather than papered over.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from aicut.errors import AicutError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Build:
    """One platform's static build, and the digest that proves it arrived intact."""

    url: str
    sha256: str
    #: Path of the binaries inside the archive, relative to its root.
    members: tuple[str, ...]
    note: str = ""


#: Static builds per platform. `sha256` empty means the digest for that build
#: has not been recorded yet - the fetch then REFUSES rather than trusting the
#: bytes, because an unverified ffmpeg is a program that runs anything the
#: network hands it. Fill these in from the publisher's own checksum file.
BUILDS: dict[str, Build] = {
    "win32": Build(
        url="https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        sha256="",
        members=("bin/ffmpeg.exe", "bin/ffprobe.exe"),
        note="gyan.dev release-essentials; the publisher posts a .sha256 beside it",
    ),
    "darwin": Build(
        url="https://evermeet.cx/ffmpeg/getrelease/zip",
        sha256="",
        members=("ffmpeg",),
        note="evermeet.cx publishes ffmpeg and ffprobe as separate archives",
    ),
    "linux": Build(
        url="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
        sha256="",
        members=("ffmpeg", "ffprobe"),
        note="johnvansickle static build; a .md5 is published beside it",
    ),
}


class FetchRefused(AicutError):
    """The download cannot be trusted or the platform has no recorded build."""


def install_dir(workspace: str | os.PathLike[str] | None = None) -> Path:
    """Where a fetched ffmpeg lives: beside the projects, not in the system."""
    if workspace is not None:
        return Path(workspace) / "tools"
    from aicut.desktop import default_workspace

    return default_workspace() / "tools"


def bundled_ffmpeg(workspace: str | os.PathLike[str] | None = None) -> Path | None:
    """The fetched ffmpeg, if one has already been installed here."""
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    candidate = install_dir(workspace) / name
    return candidate if candidate.exists() else None


def use_bundled(workspace: str | os.PathLike[str] | None = None) -> bool:
    """Put a fetched ffmpeg on PATH for this process only.

    Returns False when the system already has one - a build the user installed
    themselves is theirs, and quietly overriding it changes behaviour they did
    not ask to change.
    """
    from aicut.media.ffmpeg_util import have_ffmpeg

    if have_ffmpeg():
        return False
    fetched = bundled_ffmpeg(workspace)
    if fetched is None:
        return False
    os.environ["PATH"] = str(fetched.parent) + os.pathsep + os.environ.get("PATH", "")
    from aicut.media import ffmpeg_util

    ffmpeg_util.available_filters.cache_clear()
    return True


def platform_build() -> Build:
    key = "linux" if sys.platform.startswith("linux") else sys.platform
    build = BUILDS.get(key)
    if build is None:
        raise FetchRefused(
            f"no static ffmpeg is recorded for {sys.platform}. Install ffmpeg yourself "
            "and put it on PATH."
        )
    return build


def fetch(
    workspace: str | os.PathLike[str] | None = None,
    *,
    build: Build | None = None,
    timeout: int = 300,
) -> Path:
    """Download, verify and unpack ffmpeg. Returns the directory it landed in.

    Raises rather than installing anything whose digest was not recorded or
    does not match.
    """
    build = build or platform_build()
    if not build.sha256:
        # The conditional here used to hang off the whole concatenated string
        # rather than the last line, so a build with no note raised with an
        # EMPTY message - a refusal that tells the user nothing at all.
        message = (
            "no checksum is recorded for this platform's build, so a download cannot be "
            "verified, and an unverified ffmpeg would run whatever the network handed "
            "back.\n"
            f"  Install ffmpeg yourself, or record the digest for {build.url}"
        )
        if build.note:
            message += f"\n  ({build.note})"
        raise FetchRefused(message)

    target = install_dir(workspace)
    target.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "ffmpeg-download"
        digest = _download(build.url, archive, timeout=timeout)
        if digest != build.sha256:
            archive.unlink(missing_ok=True)
            raise FetchRefused(
                "the downloaded ffmpeg does not match its recorded checksum and was "
                f"deleted.\n  expected {build.sha256}\n  got      {digest}"
            )
        _unpack(archive, Path(tmp) / "unpacked")
        moved = _collect(Path(tmp) / "unpacked", target)

    if not moved:
        raise FetchRefused(f"the archive from {build.url} held no ffmpeg binary")
    log.info("ffmpeg installed into %s", target)
    return target


def _download(url: str, target: Path, *, timeout: int) -> str:
    """Stream to disk, hashing as it goes - the file is never held in memory."""
    sha = hashlib.sha256()
    request = Request(url, headers={"User-Agent": "aicut"})
    try:
        with urlopen(request, timeout=timeout) as response, target.open("wb") as out:
            while chunk := response.read(1 << 20):
                sha.update(chunk)
                out.write(chunk)
    except OSError as exc:
        raise FetchRefused(f"could not download ffmpeg from {url}: {exc}") from exc
    return sha.hexdigest()


def _unpack(archive: Path, into: Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            _safe_extract_zip(zf, into)
        return
    try:
        with tarfile.open(archive) as tf:
            _safe_extract_tar(tf, into)
    except tarfile.TarError as exc:
        raise FetchRefused(f"the downloaded file is neither a zip nor a tar: {exc}") from exc


def _safe_extract_zip(zf: zipfile.ZipFile, into: Path) -> None:
    for member in zf.namelist():
        _refuse_escape(member, into)
    zf.extractall(into)


def _safe_extract_tar(tf: tarfile.TarFile, into: Path) -> None:
    for member in tf.getmembers():
        _refuse_escape(member.name, into)
        if member.issym() or member.islnk():
            raise FetchRefused(f"the archive contains a link ({member.name}); refusing")
    tf.extractall(into)


def _refuse_escape(name: str, into: Path) -> None:
    """An archive entry that writes outside the directory is an attack, not a bug."""
    resolved = (into / name).resolve()
    if not str(resolved).startswith(str(into.resolve())):
        raise FetchRefused(f"the archive tries to write outside its folder ({name}); refusing")


def _collect(unpacked: Path, target: Path) -> list[Path]:
    """Find ffmpeg/ffprobe wherever the archive buried them and move them out."""
    wanted = {"ffmpeg", "ffprobe", "ffmpeg.exe", "ffprobe.exe"}
    moved: list[Path] = []
    for path in unpacked.rglob("*"):
        if path.is_file() and path.name in wanted:
            destination = target / path.name
            shutil.move(str(path), destination)
            destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
            moved.append(destination)
    return moved
