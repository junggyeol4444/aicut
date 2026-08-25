"""ffprobe wrapper: duration, streams, and the multi-track audio layout of 20장/5.2."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aicut.errors import AicutError, RenderError
from aicut.media.ffmpeg_util import require_ffmpeg, run


@dataclass
class AudioTrack:
    """One audio stream of the source.

    5.2 assumes a multi-track recording (mic / call / game / BGM on separate
    tracks), which is what makes speaker attribution cheap: only a track that
    actually carries two or more people needs diarisation.
    """

    index: int
    channels: int
    language: str = ""
    title: str = ""
    role: str = "unknown"     # mic / call / game / bgm / mixed

    @property
    def needs_diarization(self) -> bool:
        return self.role in ("call", "mixed", "unknown")


class UnusableSource(AicutError):
    """The file cannot carry a broadcast the pipeline can work on."""


@dataclass
class MediaInfo:
    path: str
    duration_sec: float
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_tracks: list[AudioTrack] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_multitrack(self) -> bool:
        return len(self.audio_tracks) > 1

    def track_by_role(self, role: str) -> AudioTrack | None:
        for track in self.audio_tracks:
            if track.role == role:
                return track
        return None

    def validate(self) -> list[str]:
        """Refuse a file that cannot be processed, and warn about one that lies.

        Better here than three stages later: 5.2 assumes picture and sound, and a
        source missing either produces a confusing failure somewhere downstream
        instead of a clear one at the door.
        """
        problems = []
        if self.duration_sec <= 0:
            problems.append("the container reports no duration")
        if not self.video_codec:
            problems.append("there is no video stream")
        if not self.audio_tracks:
            problems.append("there is no audio stream, and the passes of 5.2 read sound as well as picture")
        if problems:
            raise UnusableSource(f"{self.path}: " + "; ".join(problems))

        warnings = []
        stream_durations = [
            float(s["duration"]) for s in self.raw.get("streams", [])
            if s.get("duration") not in (None, "N/A")
        ]
        if stream_durations and self.duration_sec - max(stream_durations) > 1.0:
            warnings.append(
                f"the container claims {self.duration_sec:.1f}s but its longest stream ends at "
                f"{max(stream_durations):.1f}s - the file looks truncated, and any cut planned past "
                "that point will fail to render"
            )
        return warnings


_ROLE_HINTS = {
    "mic": ("mic", "voice", "me", "본인", "마이크", "내목소리"),
    "call": ("call", "discord", "party", "guest", "합방", "통화"),
    "game": ("game", "app", "desktop", "게임"),
    "bgm": ("bgm", "music", "brb", "배경"),
}


def classify_track(title: str, index: int, total: int) -> str:
    lowered = title.lower()
    for role, hints in _ROLE_HINTS.items():
        if any(h in lowered for h in hints):
            return role
    if total == 1:
        return "mixed"
    return "unknown"


def probe(path: str) -> MediaInfo:
    require_ffmpeg()
    if not Path(path).exists():
        raise UnusableSource(f"{path}: no such file")
    try:
        out = run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_stderr=False,
        )
    except RenderError as exc:
        # A file ffprobe cannot open is a bad input, not a rendering failure;
        # calling it the latter sends the operator looking in the wrong place.
        raise UnusableSource(f"{path}: ffprobe could not read this file\n{exc}") from exc
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise AicutError(f"ffprobe returned no usable JSON for {path}") from exc

    duration = float(data.get("format", {}).get("duration", 0.0) or 0.0)
    info = MediaInfo(path=path, duration_sec=duration, raw=data)
    audio_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not info.video_codec:
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
            info.video_codec = stream.get("codec_name", "")
            info.fps = _parse_fps(stream.get("avg_frame_rate", "0/0"))
    for position, stream in enumerate(audio_streams):
        tags = stream.get("tags", {}) or {}
        title = tags.get("title", "") or tags.get("handler_name", "")
        info.audio_tracks.append(
            AudioTrack(
                index=int(stream.get("index", position)),
                channels=int(stream.get("channels") or 0),
                language=tags.get("language", ""),
                title=title,
                role=classify_track(title, position, len(audio_streams)),
            )
        )
    return info


def verify_tail(path: str, duration_sec: float, *, margin_sec: float = 1.0) -> str | None:
    """Decode one frame near the end, to catch a file that lies about its length.

    A truncated Matroska keeps its header, so the container still reports the
    original duration while the packets stop early - which the pipeline would
    only discover when a cut planned in the missing part failed to render, hours
    into the run. One seek and one frame is cheap even on a six-hour source.

    Returns a description of the problem, or None when the tail decodes.
    """
    if duration_sec <= margin_sec:
        return None
    at = max(0.0, duration_sec - margin_sec)

    # The exit code cannot be trusted here: ffmpeg prints "File ended
    # prematurely" and still exits 0 on a truncated Matroska. Whether a frame
    # actually came out is the only reliable signal, so write one and look at it.
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "tail.jpg"
        try:
            run([
                "ffmpeg", "-hide_banner", "-v", "error", "-nostats", "-y",
                "-ss", f"{at:.3f}", "-i", path, "-frames:v", "1", str(target),
            ])
        except RenderError:
            pass
        if target.exists() and target.stat().st_size > 0:
            return None

    return (
        f"the container claims {duration_sec:.1f}s but no frame decodes at {at:.1f}s - "
        "the file is truncated, and cuts planned near the end will fail to render"
    )


def _parse_fps(rate: str) -> float:
    try:
        num, den = rate.split("/")
        den_f = float(den)
        return float(num) / den_f if den_f else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0
