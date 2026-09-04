"""ASS subtitle generation with externalised styling (10.3).

The format is fixed (ASS); the *look* is not. Font, size, colour and animation
live in a style profile under ``config/subtitle_styles`` so that 4.5's analysis of
how subtitles are actually being used on YouTube can update them. Baking one
house style into the renderer would contradict the whole point of the reference
learning loop, so the shipped profile is an initial value, not a specification.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from aicut.errors import ConfigError
from aicut.resources import SUBTITLE_STYLE_DIR
from aicut.models import SubtitleLine

STYLE_DIR = SUBTITLE_STYLE_DIR

_FIELD_ORDER = [
    "fontname", "fontsize", "primary_colour", "secondary_colour", "outline_colour", "back_colour",
    "bold", "italic", "underline", "strike_out", "scale_x", "scale_y", "spacing", "angle",
    "border_style", "outline", "shadow", "alignment", "margin_l", "margin_r", "margin_v", "encoding",
]


class SubtitleStyleProfile:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.styles: dict[str, dict[str, Any]] = data.get("styles", {})
        if "default" not in self.styles:
            raise ConfigError("a subtitle style profile must define a 'default' style")

    @classmethod
    def load(cls, name_or_path: str = "default") -> "SubtitleStyleProfile":
        path = Path(name_or_path)
        if not path.exists():
            path = STYLE_DIR / f"{name_or_path}.json"
        if not path.exists():
            raise ConfigError(f"subtitle style profile not found: {name_or_path}")
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def resolved(self, name: str) -> dict[str, Any]:
        style = self.styles.get(name)
        if style is None:
            return self.resolved("default")
        parent = style.get("inherits")
        base = dict(self.resolved(parent)) if parent else {}
        base.update({k: v for k, v in style.items() if k != "inherits"})
        return base

    @property
    def effects(self) -> dict[str, Any]:
        return self.data.get("effects", {})


def _fmt(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    centis = int(round((secs - int(secs)) * 100))
    if centis == 100:                     # rounding carried into the next second
        secs, centis = int(secs) + 1, 0
    return f"{int(hours)}:{int(minutes):02d}:{int(secs):02d}.{centis:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def build_ass(
    lines: Sequence[SubtitleLine],
    profile: SubtitleStyleProfile,
    *,
    title: str = "aicut",
) -> str:
    """Render subtitle lines (already in output time) to an ASS document."""
    used = sorted({line.style or ("emphasis" if line.emphasis else "default") for line in lines} | {"default"})
    head = [
        "[Script Info]",
        f"Title: {title}",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {profile.data.get('play_res_x', 1920)}",
        f"PlayResY: {profile.data.get('play_res_y', 1080)}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour,"
        " Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline,"
        " Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    ]
    for name in used:
        style = profile.resolved(name)
        values = ",".join(_fmt(style.get(field, 0)) for field in _FIELD_ORDER)
        head.append(f"Style: {name},{values}")

    head += [
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    effects = profile.effects
    fade_in = int(effects.get("fade_in_ms", 0))
    fade_out = int(effects.get("fade_out_ms", 0))
    transform = effects.get("emphasis_transform", "")

    for line in sorted(lines, key=lambda l: l.start_sec):
        style_name = line.style or ("emphasis" if line.emphasis else "default")
        tags = ""
        if fade_in or fade_out:
            tags += f"\\fad({fade_in},{fade_out})"
        if line.emphasis and transform:
            tags += transform
        text = (f"{{{tags}}}" if tags else "") + _escape(line.text)
        head.append(
            f"Dialogue: 0,{_timestamp(line.start_sec)},{_timestamp(line.end_sec)},{style_name},"
            f"{_escape(line.speaker)},0,0,0,,{text}"
        )
    return "\n".join(head) + "\n"


def write_ass(
    lines: Sequence[SubtitleLine],
    path: str | Path,
    profile: SubtitleStyleProfile,
    *,
    title: str = "aicut",
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_ass(lines, profile, title=title), encoding="utf-8")
    return target
