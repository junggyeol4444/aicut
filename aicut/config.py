"""Calibration profiles: every judgement threshold in the system lives here (17장).

Two rules this module exists to enforce:

1. **17.1 - externalisation.** No decision threshold may be written in code.
   Code asks the profile for a value by dotted path; if the profile has no
   value, that is an error, not a default.
2. **17.5 - no unmeasured constant is final.** A profile declares which of its
   values are still guesses in a ``provisional`` list. Reading one records the
   access, so a run can report exactly which unmeasured numbers shaped its
   output, and ``strict`` mode refuses to run on them at all.

Profiles are per channel (17.1): a different mic, game or co-stream changes the
right values, so a profile is never global truth.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from aicut.errors import ConfigError, UnmeasuredParameterError
from aicut.resources import PROFILE_DIR

log = logging.getLogger(__name__)

_MISSING = object()

DEFAULT_PROFILE_PATH = PROFILE_DIR / "default.json"


def _load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml  # optional dependency
        except ImportError as exc:  # pragma: no cover - optional dep
            raise ConfigError(f"{path} is YAML but PyYAML is not installed") from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain an object at the top level")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class CalibrationProfile:
    """A named set of judgement parameters, measured against one channel's material.

    Attributes:
        name: profile identity, normally the channel it was measured on.
        params: nested mapping of parameter values.
        provisional: dotted paths whose values are guesses, not measurements.
        measured_at: ISO timestamp of the sweep that produced the measured values.
        eval_score: 17.3 evaluation result for this profile, if it has been scored.
        notes: free text explaining how the profile was obtained.
    """

    name: str
    params: dict[str, Any] = field(default_factory=dict)
    provisional: set[str] = field(default_factory=set)
    measured: set[str] = field(default_factory=set)
    measured_at: str | None = None
    eval_score: dict[str, float] = field(default_factory=dict)
    notes: str = ""
    strict: bool = False
    source_path: Path | None = None
    _touched_provisional: set[str] = field(default_factory=set, repr=False)

    # ---- loading -----------------------------------------------------------
    @classmethod
    def from_mapping(cls, data: dict[str, Any], *, source_path: Path | None = None) -> "CalibrationProfile":
        meta = data.get("_meta", {})
        params = {k: v for k, v in data.items() if not k.startswith("_")}
        return cls(
            name=meta.get("name", "unnamed"),
            params=params,
            provisional=set(meta.get("provisional", [])),
            measured=set(meta.get("measured", [])),
            measured_at=meta.get("measured_at"),
            eval_score=meta.get("eval_score", {}) or {},
            notes=meta.get("notes", ""),
            source_path=source_path,
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None, *, strict: bool = False) -> "CalibrationProfile":
        """Load a profile, layered on top of the shipped default profile.

        A channel profile only needs to carry the values it overrides; anything
        it omits falls back to the default profile, and the provisional marks of
        both are unioned minus whatever the channel profile measured itself.
        """
        default = _load_mapping(DEFAULT_PROFILE_PATH)
        if path is None:
            merged, src = default, DEFAULT_PROFILE_PATH
        else:
            src = Path(path)
            override = _load_mapping(src)
            merged = _deep_merge(default, override)
            merged["_meta"] = _merge_meta(default.get("_meta", {}), override.get("_meta", {}))
        profile = cls.from_mapping(merged, source_path=src)
        profile.strict = strict
        return profile

    # ---- reading -----------------------------------------------------------
    def get(self, path: str, default: Any = _MISSING) -> Any:
        """Read a parameter by dotted path, e.g. ``pacing.trim_target_sec``."""
        node: Any = self.params
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is not _MISSING:
                    return default
                raise ConfigError(
                    f"profile {self.name!r} has no parameter {path!r}; "
                    "thresholds must come from the profile, never from code (17.1)"
                )
            node = node[part]
        if self.is_provisional(path):
            self._touched_provisional.add(path)
            if self.strict:
                raise UnmeasuredParameterError(
                    f"{path!r} is provisional in profile {self.name!r}; run the 17.4 sweep before strict runs"
                )
            log.debug("using provisional parameter %s=%r (profile %s)", path, node, self.name)
        return node

    def get_float(self, path: str, default: Any = _MISSING) -> float:
        value = self.get(path, default)
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"parameter {path!r} is not a number: {value!r}") from exc

    def get_int(self, path: str, default: Any = _MISSING) -> int:
        value = self.get(path, default)
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"parameter {path!r} is not an integer: {value!r}") from exc

    def is_provisional(self, path: str) -> bool:
        """True when ``path`` - or any prefix of it - is marked unmeasured.

        An explicit measurement always wins: a sweep that measured
        ``pacing.keep_score_threshold`` clears that one parameter even while the
        rest of the ``pacing`` group is still a guess.
        """
        parts = path.split(".")
        if any(".".join(parts[: i + 1]) in self.measured for i in range(len(parts))):
            return False
        return any(".".join(parts[: i + 1]) in self.provisional for i in range(len(parts)))

    # ---- reporting ---------------------------------------------------------
    def touched_provisional(self) -> list[str]:
        """Parameters actually read during this run that are still guesses."""
        return sorted(self._touched_provisional)

    def with_overrides(self, overrides: dict[str, Any], *, measured: Iterable[str] = ()) -> "CalibrationProfile":
        """Return a copy with dotted-path overrides applied (used by the sweep, 17.4)."""
        params = json.loads(json.dumps(self.params))
        for dotted, value in overrides.items():
            node = params
            parts = dotted.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        return CalibrationProfile(
            name=self.name,
            params=params,
            provisional=set(self.provisional) - set(measured),
            measured=set(self.measured) | set(measured),
            measured_at=self.measured_at,
            eval_score=dict(self.eval_score),
            notes=self.notes,
            strict=self.strict,
            source_path=self.source_path,
        )

    def to_mapping(self) -> dict[str, Any]:
        out = dict(self.params)
        out["_meta"] = {
            "name": self.name,
            "provisional": sorted(self.provisional),
            "measured": sorted(self.measured),
            "measured_at": self.measured_at,
            "eval_score": self.eval_score,
            "notes": self.notes,
        }
        return out

    def save(self, path: str | os.PathLike[str]) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_mapping(), indent=2, ensure_ascii=False), encoding="utf-8")
        return target


def _merge_meta(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Union the provisional marks; whatever the override measured stays measured."""
    measured = set(base.get("measured", [])) | set(override.get("measured", []))
    provisional = (set(base.get("provisional", [])) | set(override.get("provisional", []))) - measured
    meta = dict(base)
    meta.update(override)
    meta["provisional"] = sorted(provisional)
    meta["measured"] = sorted(measured)
    return meta
