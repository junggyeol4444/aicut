"""Audio measurement (program side).

Nothing here decides anything. It measures: where the silences are, how loud and
how busy each second is, what the integrated loudness of a rendered file is. The
levels that turn those measurements into judgements come from the calibration
profile (17장), never from this module.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from aicut.config import CalibrationProfile
from aicut.media.ffmpeg_util import require_ffmpeg, run

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")
_RMS_LEVEL = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?[\d.inf]+)")
_FRAME_PTS = re.compile(r"pts_time:([\d.]+)")


@dataclass
class Silence:
    start_sec: float
    end_sec: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


@dataclass
class LoudnessStats:
    """Result of an EBU R128 measuring pass (10.4 수정 3)."""

    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    target_offset: float = 0.0


def detect_silences(
    path: str,
    profile: CalibrationProfile,
    *,
    track_index: int | None = None,
    start_sec: float | None = None,
    duration_sec: float | None = None,
) -> list[Silence]:
    """Find silences using the profile's level and minimum duration.

    The two numbers that define "silence" for this channel are read from the
    profile precisely because they are channel-specific: a quiet condenser mic in
    a treated room and a headset in a bedroom do not share a noise floor.
    """
    require_ffmpeg()
    level = profile.get_float("silence.level_db")
    min_dur = profile.get_float("silence.min_duration_sec")
    merge_gap = profile.get_float("silence.merge_gap_sec")

    cmd = ["ffmpeg", "-hide_banner", "-nostats"]
    if start_sec is not None:
        cmd += ["-ss", f"{start_sec:.3f}"]
    if duration_sec is not None:
        cmd += ["-t", f"{duration_sec:.3f}"]
    cmd += ["-i", path]
    if track_index is not None:
        cmd += ["-map", f"0:{track_index}"]
    cmd += ["-af", f"silencedetect=noise={level}dB:d={min_dur}", "-f", "null", "-"]
    output = run(cmd)
    return _parse_silences(output, offset=start_sec or 0.0, merge_gap=merge_gap)


def _parse_silences(output: str, *, offset: float = 0.0, merge_gap: float = 0.0) -> list[Silence]:
    starts = [float(m) + offset for m in _SILENCE_START.findall(output)]
    ends = [float(m) + offset for m in _SILENCE_END.findall(output)]
    silences = [Silence(s, e) for s, e in zip(starts, ends)]
    if merge_gap <= 0 or len(silences) < 2:
        return silences
    merged = [silences[0]]
    for current in silences[1:]:
        if current.start_sec - merged[-1].end_sec <= merge_gap:
            merged[-1] = Silence(merged[-1].start_sec, current.end_sec)
        else:
            merged.append(current)
    return merged


def rms_envelope(
    path: str,
    *,
    track_index: int | None = None,
    start_sec: float = 0.0,
    duration_sec: float | None = None,
    frame_sec: float = 1.0,
) -> list[tuple[float, float]]:
    """Per-frame RMS level in dBFS as ``(time_sec, level_db)``.

    This is the raw material for the tension curve; the weighting that turns it
    into a tension value lives in :mod:`aicut.analysis.tension` with weights from
    the profile.
    """
    require_ffmpeg()
    cmd = ["ffmpeg", "-hide_banner", "-nostats"]
    if start_sec:
        cmd += ["-ss", f"{start_sec:.3f}"]
    if duration_sec is not None:
        cmd += ["-t", f"{duration_sec:.3f}"]
    cmd += ["-i", path]
    if track_index is not None:
        cmd += ["-map", f"0:{track_index}"]
    cmd += [
        "-af",
        f"asetnsamples=n={int(48000 * frame_sec)},astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
        "-f", "null", "-",
    ]
    output = run(cmd)
    return _parse_rms(output, start_sec=start_sec, frame_sec=frame_sec)


def _parse_rms(output: str, *, start_sec: float = 0.0, frame_sec: float = 1.0) -> list[tuple[float, float]]:
    times = [float(t) + start_sec for t in _FRAME_PTS.findall(output)]
    levels: list[float] = []
    for raw in _RMS_LEVEL.findall(output):
        try:
            levels.append(float(raw))
        except ValueError:
            levels.append(-120.0)          # ffmpeg prints "-inf" on digital silence
    if len(times) < len(levels):
        times += [start_sec + frame_sec * i for i in range(len(times), len(levels))]
    return list(zip(times[: len(levels)], levels))


def measure_loudness(path: str, profile: CalibrationProfile) -> LoudnessStats:
    """First pass of the two-pass EBU R128 normalisation (10.4 수정 3).

    Single-pass loudnorm adjusts dynamically and leaves the level drifting
    between sections, which on a timeline stitched from a dozen different points
    in a broadcast is exactly the artefact one notices. So measure first, then
    apply the measurement as fixed input values in the render pass.
    """
    require_ffmpeg()
    target_i = profile.get_float("render.audio.loudness.integrated_lufs")
    target_tp = profile.get_float("render.audio.loudness.true_peak_dbtp")
    target_lra = profile.get_float("render.audio.loudness.loudness_range")
    output = run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", path,
        "-af", f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json",
        "-f", "null", "-",
    ])
    return parse_loudnorm_json(output)


def parse_loudnorm_json(output: str) -> LoudnessStats:
    start = output.rfind("{")
    end = output.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("loudnorm did not print a measurement block")
    import json

    data = json.loads(output[start : end + 1])

    def num(key: str) -> float:
        value = data.get(key, "0")
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return -70.0
        return -70.0 if math.isinf(parsed) else parsed

    return LoudnessStats(
        input_i=num("input_i"),
        input_tp=num("input_tp"),
        input_lra=num("input_lra"),
        input_thresh=num("input_thresh"),
        target_offset=num("target_offset"),
    )
