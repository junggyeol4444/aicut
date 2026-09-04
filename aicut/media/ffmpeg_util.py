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
    return frozenset(_parse_filter_list(output))


def _parse_filter_list(output: str) -> set[str]:
    """Pull filter names out of `ffmpeg -filters`, whatever the layout is.

    Two earlier attempts pinned the layout and both were wrong. Selecting rows
    by "the flag column is not alphabetic" dropped the 121 filters whose flags
    read `TSC`. Pinning `^ [TSC.]{3} name in->out` then matched nothing at all
    on ffmpeg 8, which lays the table out differently - and an empty set is the
    worst outcome, because `require_filter` reads it as "this build can do
    nothing" and refuses to render.

    So key on the one thing every version prints: a column giving the stream
    signature, `V->V` or `A->A` or `N->N`. The name is the token before it. No
    assumption about indentation, flag letters, or column widths.
    """
    names = set()
    for line in output.splitlines():
        if not line[:1].isspace():
            continue                      # the "Filters:" heading starts at column 0
        parts = line.split()
        for index, token in enumerate(parts):
            if index and "->" in token:
                name = parts[index - 1]
                if _FILTER_NAME.fullmatch(name):
                    names.add(name)
                break
    return names


_FILTER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def has_filter(name: str) -> bool:
    return name in available_filters()


def filters_known() -> bool:
    """Whether the listing could be read at all.

    An empty set means the parse failed, not that ffmpeg can do nothing - and
    those are opposite instructions. Twice now a layout change has emptied it.
    """
    return bool(available_filters())


def filter_missing(name: str) -> bool:
    """True only when this build is known to lack the filter.

    Not knowing is not the same as knowing it is absent. Callers act on a
    missing filter by refusing or degrading, and doing either because the
    listing could not be parsed would break a build that was fine. When the
    answer is unknown, say no and let ffmpeg give its own error.
    """
    return filters_known() and name not in available_filters()


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
    if not filter_missing(name):
        if not filters_known():
            log.warning(
                "could not read this ffmpeg's filter list, so '%s' was not checked; "
                "continuing and letting ffmpeg answer for itself", name,
            )
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
