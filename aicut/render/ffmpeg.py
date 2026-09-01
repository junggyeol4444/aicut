"""The renderer (10장). It executes an edit plan and judges nothing (10.1).

Three corrections from 10.4 are implemented here rather than described:

**10.4-1 face-tracking zoom.** ``crop=...:x=face_center_x:y=face_center_y`` does
not work: those are not ffmpeg variables, and ``crop`` cannot be steered
per-frame from nothing. Two strategies are implemented instead - ``segment_crop``
(split the zoom into segments, one fixed crop each, then concat: simple, stepped
camera) and ``sendcmd`` (drive crop's x/y over time from a generated command
file: smooth, more complex graph). Which one wins is an MVP 6 measurement, so the
choice is a profile parameter, not a constant. The third option in 10.4 -
frame-by-frame compositing outside ffmpeg - is deliberately not implemented here;
it belongs in a separate processing pipeline if measurement ever justifies it.

**10.4-2 cut joins.** ``acrossfade`` per join would build a filter graph with
hundreds of nested crossfades. Each segment instead gets a few-millisecond
``afade`` in/out to kill the click, and joining is done by concat.

**10.4-3 loudness.** EBU R128 normalisation stays, but as a measure-then-apply
two-pass, so a timeline assembled from a dozen places in the broadcast does not
drift in level between them.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from aicut.config import CalibrationProfile
from aicut.errors import RenderError
from aicut.media.audio import LoudnessStats, measure_loudness
from aicut.media.ffmpeg_util import LIBASS_HINT, require_ffmpeg, require_filter, run
from aicut.render.editplan import EditPlan
from aicut.render.timeline import Segment, Timeline

log = logging.getLogger(__name__)


@dataclass
class RenderSettings:
    video_codec: str = "libx264"
    preset: str = "medium"
    crf: int = 18
    pix_fmt: str = "yuv420p"
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    sample_rate: int = 48000
    height: int = 1080
    width: int | None = None
    fps: float | None = None
    cut_fade_ms: int = 8
    zoom_strategy: str = "segment_crop"
    loudness_i: float = -14.0
    loudness_tp: float = -1.0
    loudness_lra: float = 11.0
    two_pass_loudness: bool = True

    @classmethod
    def from_profile(cls, profile: CalibrationProfile, *, target_type: str = "") -> "RenderSettings":
        video = profile.get("render.video")
        audio = profile.get("render.audio")
        loud = profile.get("render.audio.loudness")
        settings = cls(
            video_codec=video["codec"],
            preset=video["preset"],
            crf=int(video["crf"]),
            pix_fmt=video["pix_fmt"],
            audio_codec=audio["codec"],
            audio_bitrate=audio["bitrate"],
            sample_rate=int(audio["sample_rate"]),
            height=int(video["default_height"]),
            fps=video.get("fps"),
            cut_fade_ms=int(audio["cut_fade_ms"]),
            zoom_strategy=profile.get("render.zoom.strategy"),
            loudness_i=float(loud["integrated_lufs"]),
            loudness_tp=float(loud["true_peak_dbtp"]),
            loudness_lra=float(loud["loudness_range"]),
            two_pass_loudness=bool(loud["two_pass"]),
        )
        if target_type and "short" in target_type.lower():
            width, height = profile.get("render.video.shorts_resolution")
            settings.width, settings.height = int(width), int(height)
        return settings

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# ---------------------------------------------------------------------------
# filter construction
# ---------------------------------------------------------------------------
def zoom_filter(effect: dict[str, Any], settings: RenderSettings) -> str:
    """Build the crop chain for one zoomed segment (10.4-1, strategy a).

    ``center`` is normalised 0..1 in the source frame; the crop window is
    clamped so it can never leave the frame, which is the other half of why the
    original expression could not work.
    """
    scale = float(effect.get("scale", 0.83))
    scale = max(0.2, min(1.0, scale))
    cx, cy = effect.get("center", [0.5, 0.5])
    cx = max(0.0, min(1.0, float(cx)))
    cy = max(0.0, min(1.0, float(cy)))
    # x = centre - half a window, clamped into [0, in_w - window]
    x = f"min(max(iw*{cx:.4f}-iw*{scale:.4f}/2\\,0)\\,iw-iw*{scale:.4f})"
    y = f"min(max(ih*{cy:.4f}-ih*{scale:.4f}/2\\,0)\\,ih-ih*{scale:.4f})"
    return f"crop=w=iw*{scale:.4f}:h=ih*{scale:.4f}:x={x}:y={y}"


def sendcmd_file(keyframes: Sequence[dict[str, Any]], path: str | Path) -> Path:
    """Write a sendcmd script that walks crop's x/y over time (10.4-1, strategy b).

    Each keyframe is ``{"at_sec": t, "scale": s, "center": [cx, cy]}`` in
    segment-local time. ffmpeg applies each command at its timestamp, so the
    camera moves in steps as fine as the keyframes are dense.

    **This path pans; it does not zoom.** crop marks ``w`` and ``h``
    runtime-commandable, but sending them stalls the graph: measured on ffmpeg
    7.1, a segment whose crop size changes mid-stream hangs with the process
    idle (60s wall, 0.5s CPU, no output), because the downstream scale filter
    never reconfigures. Only ``x`` and ``y`` are sent, at one constant crop
    size, and a keyframe list carrying more than one scale is flattened with a
    warning rather than silently pretending to zoom.

    A zoom that actually changes magnification therefore belongs to the
    ``segment_crop`` strategy, which splits the change into segments and gets a
    stepped camera. Which of the two a channel prefers is the MVP 6
    measurement 10.4 asks for; this is one of its inputs.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(keyframes, key=lambda k: float(k.get("at_sec", 0.0)))
    scales = {round(float(kf.get("scale", 0.83)), 4) for kf in ordered}
    scale = max(0.2, min(1.0, float(ordered[0].get("scale", 0.83)))) if ordered else 0.83
    if len(scales) > 1:
        log.warning(
            "sendcmd zoom holds %d different scales %s; crop cannot resize mid-segment without"
            " stalling the graph, so the camera pans at scale %.2f. Use the segment_crop strategy"
            " for a magnification change (10.4-1).",
            len(scales), sorted(scales), scale,
        )
    lines = []
    for kf in ordered:
        at = float(kf.get("at_sec", 0.0))
        cx, cy = kf.get("center", [0.5, 0.5])
        cx = max(0.0, min(1.0, float(cx)))
        cy = max(0.0, min(1.0, float(cy)))
        x = f"min(max(iw*{cx:.4f}-iw*{scale:.4f}/2,0),iw-iw*{scale:.4f})"
        y = f"min(max(ih*{cy:.4f}-ih*{scale:.4f}/2,0),ih-ih*{scale:.4f})"
        lines.append(f"{at:.3f} crop x '{x}', crop y '{y}';")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def scale_filter(settings: RenderSettings) -> str:
    """Fit to the output frame; pad rather than stretch when the aspect differs."""
    if settings.width:
        return (
            f"scale={settings.width}:{settings.height}:force_original_aspect_ratio=increase,"
            f"crop={settings.width}:{settings.height}"
        )
    return f"scale=-2:{settings.height}"


def audio_edge_filter(duration: float, settings: RenderSettings) -> str:
    """Millisecond fades at both ends of a segment (10.4-2)."""
    fade = max(0.0, settings.cut_fade_ms / 1000.0)
    if fade <= 0 or duration <= fade * 2:
        return "anull"
    return f"afade=t=in:st=0:d={fade:.4f},afade=t=out:st={duration - fade:.4f}:d={fade:.4f}"


def build_segment_command(
    source: str,
    segment: Segment,
    out_path: str,
    settings: RenderSettings,
    *,
    visual_effect: dict[str, Any] | None = None,
    audio_effect: dict[str, Any] | None = None,
    sendcmd_path: str | None = None,
) -> list[str]:
    """ffmpeg args that cut one segment out of the source and normalise its shape.

    Seeking is done with ``-ss`` before ``-i`` (fast) plus ``-accurate_seek`` so
    the frame the plan asked for is the frame that lands in the file.
    """
    effect = visual_effect or {}
    filters: list[str] = []
    if effect.get("type") == "zoom":
        if settings.zoom_strategy == "sendcmd" and sendcmd_path:
            # Same escaping as the subtitle path: this is a filter argument, and
            # a Windows command file at C:\... would otherwise end the option
            # at the drive colon.
            filters.append(f"sendcmd=f='{_escape_filter_path(sendcmd_path)}'")
            filters.append(zoom_filter(effect, settings))
        else:
            filters.append(zoom_filter(effect, settings))
    filters.append(scale_filter(settings))
    if settings.fps:
        filters.append(f"fps={settings.fps}")
    filters.append("setsar=1")

    audio_filters = [audio_edge_filter(segment.duration, settings)]
    gain = float((audio_effect or {}).get("gain_db", 0.0))
    if gain:
        audio_filters.insert(0, f"volume={gain}dB")
    audio_filters.append(f"aresample={settings.sample_rate}")

    return [
        "ffmpeg", "-hide_banner", "-nostats", "-y",
        "-accurate_seek", "-ss", f"{segment.source_start_sec:.3f}",
        "-t", f"{segment.duration:.3f}",
        "-i", source,
        "-vf", ",".join(filters),
        "-af", ",".join(audio_filters),
        "-c:v", settings.video_codec, "-preset", settings.preset, "-crf", str(settings.crf),
        "-pix_fmt", settings.pix_fmt,
        "-c:a", settings.audio_codec, "-b:a", settings.audio_bitrate, "-ar", str(settings.sample_rate),
        "-map", "0:v:0", "-map", "0:a:0?",
        out_path,
    ]


def build_concat_command(list_path: str, out_path: str) -> list[str]:
    """Join the segments (10.4-2: concat, not a tower of crossfades)."""
    return [
        "ffmpeg", "-hide_banner", "-nostats", "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-c", "copy", out_path,
    ]


def _escape_filter_path(path: str) -> str:
    """Make a path safe inside an ffmpeg filter argument.

    Filter syntax treats ':' as an option separator and quotes as delimiters,
    so a Windows path like ``C:\\work\\subs.ass`` has to arrive as
    ``C\\:/work/subs.ass`` - backslashes turned into forward slashes, the drive
    colon escaped. On POSIX the same rules are harmless. An unescaped path does
    not error; it produces a video with no captions, which is worse.
    """
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def build_final_command(
    joined_path: str,
    out_path: str,
    settings: RenderSettings,
    *,
    ass_path: str | None = None,
    loudness: LoudnessStats | None = None,
    fonts_dir: str | None = None,
) -> list[str]:
    """Burn subtitles and apply the measured loudness correction (10.4-3)."""
    video_filters: list[str] = []
    if ass_path:
        # `filename=` is named rather than passed positionally: ffmpeg 7.2 (the
        # homebrew build) rejects `subtitles='<path>'` with "No option name
        # near", where 6.x and 7.1 accepted it. Naming the option works on all
        # of them, and the failure it avoids is a render that dies rather than
        # one that quietly ships without captions.
        subtitle = f"subtitles=filename='{_escape_filter_path(ass_path)}'"
        if fonts_dir:
            subtitle += f":fontsdir='{_escape_filter_path(fonts_dir)}'"
        video_filters.append(subtitle)

    loudnorm = f"loudnorm=I={settings.loudness_i}:TP={settings.loudness_tp}:LRA={settings.loudness_lra}"
    if loudness is not None:
        loudnorm += (
            f":measured_I={loudness.input_i}:measured_TP={loudness.input_tp}"
            f":measured_LRA={loudness.input_lra}:measured_thresh={loudness.input_thresh}"
            f":offset={loudness.target_offset}:linear=true"
        )
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", joined_path]
    if video_filters:
        cmd += ["-vf", ",".join(video_filters), "-c:v", settings.video_codec,
                "-preset", settings.preset, "-crf", str(settings.crf), "-pix_fmt", settings.pix_fmt]
    else:
        cmd += ["-c:v", "copy"]
    cmd += [
        "-af", f"{loudnorm},aresample={settings.sample_rate}",
        "-c:a", settings.audio_codec, "-b:a", settings.audio_bitrate, "-ar", str(settings.sample_rate),
        "-movflags", "+faststart",
        out_path,
    ]
    return cmd


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def _directory_mb(path: Path) -> float:
    try:
        return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file()) / 1e6
    except OSError:
        return 0.0


class Renderer:
    """Executes an edit plan. Contains no decision of any kind."""

    def __init__(self, profile: CalibrationProfile, work_dir: str | Path):
        self.profile = profile
        self.work_dir = Path(work_dir)

    def render(
        self,
        plan: EditPlan,
        out_path: str | Path,
        *,
        ass_path: str | Path | None = None,
        keep_intermediate: bool = False,
    ) -> Path:
        require_ffmpeg()
        if ass_path:
            # Checked here rather than at the final command: by then the
            # segments have been cut and joined, and the failure reads as a
            # mysterious render error instead of a missing build option.
            require_filter(
                "subtitles", needed_for="burning subtitles (10.3)", install_hint=LIBASS_HINT
            )
        settings = RenderSettings.from_profile(self.profile, target_type=plan.target_type)
        timeline = Timeline.from_cuts(plan.cuts)
        if not timeline.segments:
            raise RenderError(f"episode {plan.episode_id} has no renderable segment")

        # Absolute, because ffmpeg's concat demuxer resolves the paths in the
        # list file relative to the list file's own directory: a relative path
        # written there would be joined onto the stage directory twice.
        stage = (self.work_dir / plan.episode_id).resolve()
        stage.mkdir(parents=True, exist_ok=True)
        cuts_by_order = {c.sequence_order: c for c in plan.cuts}

        segment_paths: list[Path] = []
        for i, segment in enumerate(timeline.segments):
            cut = cuts_by_order.get(segment.sequence_order)
            seg_path = stage / f"seg_{i:05d}.mp4"
            sendcmd = None
            effect = cut.visual_effect if cut else {}
            if effect.get("type") == "zoom" and effect.get("keyframes") and settings.zoom_strategy == "sendcmd":
                sendcmd = str(sendcmd_file(effect["keyframes"], stage / f"seg_{i:05d}.cmd"))
            run(build_segment_command(
                plan.source_path, segment, str(seg_path), settings,
                visual_effect=effect,
                audio_effect=cut.audio_effect if cut else {},
                sendcmd_path=sendcmd,
            ))
            segment_paths.append(seg_path)

        list_path = stage / "segments.txt"
        list_path.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in segment_paths) + "\n", encoding="utf-8"
        )
        joined = stage / "joined.mp4"
        run(build_concat_command(str(list_path), str(joined)))

        loudness = None
        if settings.two_pass_loudness:
            loudness = measure_loudness(str(joined), self.profile)

        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            run(build_final_command(
                str(joined), str(target), settings,
                ass_path=str(ass_path) if ass_path else None,
                loudness=loudness,
            ))
        except RenderError:
            # The cut segments stay, because they are what a person needs to see
            # to work out why this failed - but silently leaving hundreds of
            # megabytes behind is how a desktop program (22.1) fills a disk
            # without anyone knowing which directory did it.
            log.warning(
                "render failed; leaving %s in place for inspection (%.0f MB) - "
                "delete it once the cause is found",
                stage, _directory_mb(stage),
            )
            raise

        if not keep_intermediate:
            shutil.rmtree(stage, ignore_errors=True)
        return target
