"""Hand the edit plan to a video editor instead of rendering it (22.1, 22.5).

An edit plan is a decision about which pieces of a broadcast belong in a video
and in what order. Rendering it here is one way to use that; the other is to
open it in the editor the channel already works in and take it from there. 22.5
wants a person able to disagree with a decision and change it, and the place
most people do that is their timeline, not a JSON file.

Three formats, chosen because between them they cover every editor a streamer
is likely to have:

* **EDL** (CMX 3600) - the oldest and the most widely read. Cuts only.
* **FCPXML** - Premiere Pro, DaVinci Resolve and Final Cut all import it, and
  unlike EDL it carries the source file path, so the timeline relinks itself.
* **SRT** - subtitles as a sidecar, because neither of the other two carries
  them in a form editors agree on.

Nothing here renders or re-encodes: these describe the same cuts the renderer
would make, so a person can compare the two.
"""

from __future__ import annotations

import html
import math
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

from aicut.errors import AicutError
from aicut.render.editplan import EditPlan
from aicut.render.timeline import Segment, Timeline

#: `C:\...` or `C:/...` - an absolute path on Windows, wherever this runs.
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")

#: Frame rates an EDL timecode can be written for without lying about it.
#: 23.976, 29.97 and 59.94 are counted at the nearest whole rate, which is what
#: non-drop-frame timecode is; drop-frame is not emitted at all rather than
#: emitted wrongly.
_TC_BASE = {23.976: 24, 24: 24, 25: 25, 29.97: 30, 30: 30, 50: 50, 59.94: 60, 60: 60}


class UnsupportedFrameRate(AicutError):
    """The frame rate cannot be written as timecode without inventing frames."""


def timecode(seconds: float, fps: float) -> str:
    """HH:MM:SS:FF, non-drop-frame.

    A negative or absurd input is a bug upstream, so it fails here rather than
    silently wrapping into a plausible-looking timecode hours away.
    """
    if seconds < 0:
        raise ValueError(f"negative time {seconds}")
    base = _tc_base(fps)
    total = int(round(seconds * base))
    frames = total % base
    total //= base
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}:{frames:02d}"


def _tc_base(fps: float) -> int:
    for known, base in _TC_BASE.items():
        if math.isclose(fps, known, rel_tol=1e-3):
            return base
    if float(fps).is_integer() and 1 <= fps <= 240:
        return int(fps)
    raise UnsupportedFrameRate(
        f"{fps} fps has no non-drop-frame timecode this writes. Pass --fps with the "
        "rate the editor's sequence uses (24, 25, 30, 50, 60, or 23.976/29.97/59.94)."
    )


def _segments(plan: EditPlan) -> list[Segment]:
    segments = Timeline.from_cuts(plan.cuts).segments
    if not segments:
        raise AicutError(f"edit plan {plan.episode_id} has no segment to export")
    return segments


def plan_fps(plan: EditPlan, override: float | None = None) -> float:
    """The rate to write timecode at: what was asked for, then what the plan says."""
    if override:
        return float(override)
    fps = (plan.render_settings or {}).get("fps")
    if fps:
        return float(fps)
    raise AicutError(
        "this plan does not record a frame rate, so timecode cannot be written. "
        "Pass --fps with the rate the editor's sequence uses."
    )


# ---------------------------------------------------------------------------
def to_edl(plan: EditPlan, fps: float, *, title: str | None = None) -> str:
    """CMX 3600. Cuts only - no effects, no subtitles, no source path.

    An EDL identifies its source by reel name, not by file, so the editor asks
    which clip this is on import. That is the format's limit, not a shortcut;
    FCPXML is the one that relinks itself.
    """
    reel = _reel_name(plan.source_path)
    lines = [
        f"TITLE: {title or plan.episode_id}",
        "FCM: NON-DROP FRAME",
    ]
    for index, segment in enumerate(_segments(plan), start=1):
        src_in = timecode(segment.source_start_sec, fps)
        src_out = timecode(segment.source_end_sec, fps)
        rec_in = timecode(segment.out_start_sec, fps)
        rec_out = timecode(segment.out_start_sec + segment.duration, fps)
        lines.append(f"{index:03d}  {reel} AA/V  C        {src_in} {src_out} {rec_in} {rec_out}")
        lines.append(f"* FROM CLIP NAME: {source_name(plan.source_path)}")
    return "\n".join(lines) + "\n"


def _reel_name(source_path: str) -> str:
    """EDL reel names are 8 characters of A-Z0-9 in practice; anything else
    gets mangled differently by every editor, so it is normalised here."""
    name = source_name(source_path)
    stem = name.rpartition(".")[0] or name
    cleaned = "".join(c for c in stem.upper() if c.isascii() and c.isalnum())
    return (cleaned or "AICUT")[:8].ljust(8)


# ---------------------------------------------------------------------------

def source_name(path: str) -> str:
    """The file's own name, whichever platform wrote the path.

    `Path(...).name` reads the separators of the machine it runs on, so a
    Windows path in a plan opened on Linux keeps its whole `C:\\Users\\...`
    prefix as the "name" - which then goes in the timeline as the clip label.
    """
    return path.replace("\\", "/").rstrip("/").rpartition("/")[2] or path


def media_src(path: str) -> str:
    """The `src` of a media-rep: a URL, percent-encoded, on every platform.

    `Path.as_uri()` cannot do this job. It refuses relative paths, and its idea
    of "absolute" is the platform's: a plan written on Linux carries
    `/broadcasts/x.mkv`, which Windows reads as relative, so the export fell
    through to a bare path with raw spaces in it - an importer then either
    fails to relink or silently takes the space as the end of the name. Windows
    CI caught exactly that.

    So the shape is read from the string, not from the running platform.
    """
    if "://" in path:                                  # already a URL
        return path
    windows_absolute = bool(_DRIVE.match(path))
    text = path.replace("\\", "/") if windows_absolute or "\\" in path else path
    # ':' stays literal so a drive letter survives; everything else that is not
    # URL-safe is encoded, which is what puts %20 in place of a space.
    quoted = quote(text, safe="/:")
    if windows_absolute:
        return "file:///" + quoted
    if text.startswith("/"):
        return "file://" + quoted
    return quoted                                      # relative: a relative URL


def to_fcpxml(
    plan: EditPlan,
    fps: float,
    *,
    source_duration_sec: float | None = None,
    source_size: tuple[int, int] | None = None,
) -> str:
    """FCPXML 1.9, which Premiere Pro, DaVinci Resolve and Final Cut all read.

    Times are rational (`N/Ds`) because the format is frame-exact and decimal
    seconds do not land on frame boundaries for 23.976 or 29.97. Everything is
    quantised to the frame here rather than left for the importer to round in
    whichever direction it prefers.
    """
    base = _tc_base(fps)
    ntsc = not float(fps).is_integer()
    # 30000/1001 for 29.97, 30/1 for 30 - the form every FCPXML importer expects.
    frame_num, frame_den = (1001, base * 1000) if ntsc else (1, base)

    def rational(seconds: float) -> str:
        frames = int(round(seconds * (frame_den / frame_num)))
        return f"{frames * frame_num}/{frame_den}s"

    segments = _segments(plan)
    total = sum(s.duration for s in segments)
    src_dur = source_duration_sec or max(s.source_end_sec for s in segments)
    name = source_name(plan.source_path)
    stem = name.rpartition(".")[0] or name
    src_uri = media_src(plan.source_path)

    seq_w = plan.render_settings.get("width") or 1920
    seq_h = plan.render_settings.get("height") or 1080
    # The asset carries the SOURCE's shape, not the sequence's. Pointing a
    # 1920x1080 clip at a 1080x1920 vertical format makes the importer letterbox
    # or stretch it, which looks like the plan asked for that.
    src_w, src_h = source_size or (seq_w, seq_h)

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE fcpxml>',
        '<fcpxml version="1.9">',
        "  <resources>",
        f'    <format id="r1" name="AicutSequence" frameDuration="{frame_num}/{frame_den}s"'
        f' width="{seq_w}" height="{seq_h}"/>',
        f'    <format id="r3" name="AicutSource" frameDuration="{frame_num}/{frame_den}s"'
        f' width="{src_w}" height="{src_h}"/>',
        f'    <asset id="r2" name="{html.escape(name)}" start="0s"'
        f' duration="{rational(src_dur)}" hasVideo="1" hasAudio="1" format="r3">',
        f'      <media-rep kind="original-media" src="{html.escape(src_uri)}"/>',
        "    </asset>",
        "  </resources>",
        "  <library>",
        f'    <event name="{html.escape(plan.project_id or "aicut")}">',
        f'      <project name="{html.escape(plan.episode_id)}">',
        f'        <sequence format="r1" duration="{rational(total)}"'
        ' tcStart="0s" tcFormat="NDF">',
        "          <spine>",
    ]
    for segment in segments:
        out.append(
            f'            <asset-clip ref="r2" name="{html.escape(stem)}"'
            f' offset="{rational(segment.out_start_sec)}"'
            f' start="{rational(segment.source_start_sec)}"'
            f' duration="{rational(segment.duration)}"/>'
        )
    out += [
        "          </spine>",
        "        </sequence>",
        "      </project>",
        "    </event>",
        "  </library>",
        "</fcpxml>",
    ]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
def to_srt(plan: EditPlan) -> str:
    """Subtitles on the output clock, which is what an editor's timeline uses."""
    lines: list[str] = []
    for index, line in enumerate(sorted(plan.subtitles, key=lambda s: s.start_sec), start=1):
        lines.append(str(index))
        lines.append(f"{_srt_time(line.start_sec)} --> {_srt_time(line.end_sec)}")
        lines.append(line.text)
        lines.append("")
    return "\n".join(lines)


def _srt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    milli = int(round(seconds * 1000))
    return (
        f"{milli // 3600000:02d}:{(milli % 3600000) // 60000:02d}:"
        f"{(milli % 60000) // 1000:02d},{milli % 1000:03d}"
    )


# ---------------------------------------------------------------------------
FORMATS = {"edl": "edl", "fcpxml": "fcpxml", "srt": "srt"}


def export(
    plan: EditPlan,
    out_path: str | Path,
    *,
    fmt: str,
    fps: float | None = None,
    source_size: tuple[int, int] | None = None,
    source_duration_sec: float | None = None,
) -> Path:
    """Write one exchange file for this plan."""
    if fmt not in FORMATS:
        raise AicutError(f"unknown export format {fmt!r}; choose from {', '.join(sorted(FORMATS))}")
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "srt":
        text = to_srt(plan)
    else:
        rate = plan_fps(plan, fps)
        text = to_edl(plan, rate) if fmt == "edl" else to_fcpxml(
            plan, rate, source_duration_sec=source_duration_sec, source_size=source_size,
        )
    target.write_text(text, encoding="utf-8")
    return target
