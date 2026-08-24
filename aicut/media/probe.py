"""ffprobe wrapper: duration, streams, and the multi-track audio layout of 20장/5.2."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from aicut.errors import AicutError
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
    out = run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_stderr=False,
    )
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


def _parse_fps(rate: str) -> float:
    try:
        num, den = rate.split("/")
        den_f = float(den)
        return float(num) / den_f if den_f else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0
