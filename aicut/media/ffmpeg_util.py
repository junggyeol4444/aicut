"""Shared ffmpeg/ffprobe process handling."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from typing import Sequence

from aicut.errors import RenderError

log = logging.getLogger(__name__)


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def require_ffmpeg() -> None:
    if not have_ffmpeg():
        raise RenderError("ffmpeg and ffprobe must be on PATH (20.1)")


@lru_cache(maxsize=None)
def available_filters() -> frozenset[str]:
    """Filters this ffmpeg build actually has.

    Builds differ in what they were compiled with, and the difference is not
    academic: a homebrew ffmpeg without libass has no `subtitles` filter at
    all, so burning captions fails with "No such filter" after the render has
    already done its work.
    """
    if not have_ffmpeg():
        return frozenset()
    try:
        output = run(["ffmpeg", "-hide_banner", "-filters"], timeout=30)
    except (RenderError, OSError):
        return frozenset()
    return frozenset(m.group(1) for m in _FILTER_LINE.finditer(output))


# " TSC name  A->A  description" - the flag column is timeline / slice-threading
# / command support, each either its letter or a dot. Matching the arrow column
# too is what separates a filter row from the legend above it.
_FILTER_LINE = re.compile(r"^ [TSC.]{3} (\S+) +\S+->\S+ ", re.MULTILINE)


def has_filter(name: str) -> bool:
    return name in available_filters()


#: How to get a build with libass on each platform. Homebrew split its formula:
#: plain `ffmpeg` no longer links libass, so the most common macOS install
#: produces a working ffmpeg that cannot burn a single caption.
LIBASS_HINT = {
    "darwin": "Homebrew's plain `ffmpeg` bottle is built without libass. "
              "`brew install ffmpeg-full` (or `brew reinstall --build-from-source ffmpeg`) has it.",
    "win32": "Install a full build: `choco install ffmpeg-full`, or the 'full' build from gyan.dev.",
}.get(sys.platform, "Install the full ffmpeg package (Debian/Ubuntu: `sudo apt install ffmpeg`).")


def require_filter(name: str, *, needed_for: str, install_hint: str) -> None:
    """Fail before the work, with the fix, when a build lacks what we need."""
    if has_filter(name):
        return
    raise RenderError(
        f"this ffmpeg build has no '{name}' filter, which {needed_for} requires.\n{install_hint}"
    )


def run(cmd: Sequence[str], *, capture_stderr: bool = True, timeout: float | None = None) -> str:
    """Run a command, returning stdout (or stderr, where ffmpeg writes its measurements)."""
    log.debug("run: %s", " ".join(cmd))
    proc = subprocess.run(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-2000:]
        raise RenderError(f"command failed ({proc.returncode}): {' '.join(cmd[:6])} ...\n{tail}")
    out = proc.stdout.decode("utf-8", "replace")
    if capture_stderr:
        out += proc.stderr.decode("utf-8", "replace")
    return out
