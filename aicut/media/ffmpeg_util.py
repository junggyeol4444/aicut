"""Shared ffmpeg/ffprobe process handling."""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Sequence

from aicut.errors import RenderError

log = logging.getLogger(__name__)


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def require_ffmpeg() -> None:
    if not have_ffmpeg():
        raise RenderError("ffmpeg and ffprobe must be on PATH (20.1)")


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
