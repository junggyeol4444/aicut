"""Smart pacing: deciding what a silence is for (9장).

This is the direct answer to 1.2 (2) - the failure mode where an editor kills
every gap by rule and destroys the beat that made the moment funny. Two silences
of identical length and identical level can be opposite things: one is the
streamer too stunned to speak, the other is the streamer walking to a quest
marker. Length alone cannot tell them apart, so the judgement reads context:

* how loud the moment right *before* the silence was (a scream then silence is a
  reaction; quiet then silence is dead air),
* whether the silence sits on a speaker handover (someone is waiting to answer),
* whether the person on screen is frozen or moving,
* what role the containing cut was given by the edit plan (8.2).

No threshold appears in this file. The rule layer scores from profile parameters
and hands the score, the signals and its own suggestion to the reasoning layer,
which may override it - and 9.4 requires both to be scored against human-edited
material (17.3) rather than trusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from aicut.analysis.tension import TensionCurve
from aicut.config import CalibrationProfile
from aicut.llm import Producer
from aicut.media.audio import Silence
from aicut.media.vision import MotionSample, stillness
from aicut.models import PacingMode, UNKNOWN_SPEAKER, Utterance


@dataclass
class SilenceContext:
    """Everything known about one silence, before anyone judges it."""

    start_sec: float
    end_sec: float
    preceding_tension: float = 0.0
    following_tension: float = 0.0
    speaker_before: str = UNKNOWN_SPEAKER
    speaker_after: str = UNKNOWN_SPEAKER
    motion: float = 0.0
    scene_role: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)

    @property
    def is_speaker_handover(self) -> bool:
        return (
            self.speaker_before != UNKNOWN_SPEAKER
            and self.speaker_after != UNKNOWN_SPEAKER
            and self.speaker_before != self.speaker_after
        )


@dataclass
class PacingDecision:
    silence: SilenceContext
    mode: PacingMode
    reason: str
    score: float = 0.0
    signals: dict[str, float | str | bool] = field(default_factory=dict)
    decided_by: str = "rule"

    @property
    def trimmed_span(self) -> tuple[float, float] | None:
        """The span the renderer should actually remove, or None for KEEP."""
        if self.mode is PacingMode.KEEP:
            return None
        return (self.silence.start_sec, self.silence.end_sec)


def build_silence_contexts(
    silences: Sequence[Silence],
    utterances: Sequence[Utterance],
    tension: TensionCurve,
    motion: Sequence[MotionSample],
    profile: CalibrationProfile,
    *,
    scene_role: str = "",
) -> list[SilenceContext]:
    """Attach context to raw silences so they can be judged rather than measured."""
    look_back = profile.get_float("pacing.preroll_tension_window_sec")
    out: list[SilenceContext] = []
    for s in silences:
        before = [u for u in utterances if u.end_sec <= s.start_sec + 0.05]
        after = [u for u in utterances if u.start_sec >= s.end_sec - 0.05]
        out.append(
            SilenceContext(
                start_sec=s.start_sec,
                end_sec=s.end_sec,
                preceding_tension=tension.peak(s.start_sec - look_back, s.start_sec),
                following_tension=tension.peak(s.end_sec, s.end_sec + look_back),
                speaker_before=before[-1].speaker if before else UNKNOWN_SPEAKER,
                speaker_after=after[0].speaker if after else UNKNOWN_SPEAKER,
                motion=stillness(list(motion), s.start_sec, s.end_sec),
                scene_role=scene_role,
            )
        )
    return out


class PacingJudge:
    """Scores a silence from profile parameters, then optionally asks the producer."""

    def __init__(self, profile: CalibrationProfile, producer: Producer | None = None):
        self.profile = profile
        self.producer = producer

    def judge(self, ctx: SilenceContext) -> PacingDecision:
        p = self.profile
        high = p.get_float("tension.high")
        grace = p.get_float("pacing.speaker_switch_grace_sec")
        keep_max = p.get_float("pacing.keep_max_sec")
        cut_min = p.get_float("pacing.cut_min_sec")
        still_max = p.get_float("pacing.still_frame_motion_max")
        role_bias = p.get("pacing.role_keep_bias", {})
        keep_threshold = p.get_float("pacing.keep_score_threshold")
        weight = p.get("pacing.keep_signal_weights")

        signals: dict[str, float | str | bool] = {
            "duration_sec": round(ctx.duration, 3),
            "preceding_tension": round(ctx.preceding_tension, 3),
            "speaker_handover": ctx.is_speaker_handover,
            "motion": round(ctx.motion, 4),
            "scene_role": ctx.scene_role,
        }

        # Each term is "a reason to keep this silence"; they sum into a score the
        # profile's threshold turns into a verdict. Both the terms' weights and
        # the threshold live in the profile - a rule that says how much a
        # speaker handover is worth is exactly the kind of judgement 17.1 keeps
        # out of code.
        score = 0.0
        reasons: list[str] = []
        if ctx.preceding_tension >= high:
            score += float(weight["high_tension_preroll"])
            reasons.append("high-tension moment immediately before")
        if ctx.is_speaker_handover and ctx.duration <= grace:
            score += float(weight["speaker_handover"])
            reasons.append("waiting on a speaker handover")
        if ctx.motion <= still_max and ctx.preceding_tension >= high:
            score += float(weight["frozen_after_peak"])
            reasons.append("person frozen on screen after a loud beat")
        if ctx.duration > keep_max:
            score += float(weight["over_keep_max"])
            reasons.append("longer than this channel's keepable beat")
        if ctx.duration >= cut_min and ctx.preceding_tension < high:
            score += float(weight["long_and_low"])
            reasons.append("long and low-energy")
        score += float(role_bias.get(ctx.scene_role, 0.0))

        if score >= keep_threshold and ctx.duration <= keep_max:
            mode = PacingMode.KEEP
        elif ctx.duration >= cut_min and score < keep_threshold:
            mode = PacingMode.CUT
        else:
            mode = PacingMode.TRIM
        signals["score"] = round(score, 3)

        decision = PacingDecision(
            silence=ctx,
            mode=mode,
            reason="; ".join(reasons) or "no keep signal",
            score=score,
            signals=signals,
        )
        return self._consult(decision)

    def _consult(self, decision: PacingDecision) -> PacingDecision:
        """Let the reasoning layer overrule the rule layer (9장, 18장)."""
        if self.producer is None:
            return decision
        answer = self.producer.judge_pacing({
            "silence": {
                "start_sec": decision.silence.start_sec,
                "end_sec": decision.silence.end_sec,
                "duration_sec": decision.silence.duration,
            },
            "signals": decision.signals,
            "rule_suggestion": decision.mode.value,
            "rule_reason": decision.reason,
        })
        try:
            mode = PacingMode(str(answer.get("pacing_mode", decision.mode.value)).upper())
        except ValueError:
            return decision
        if mode is decision.mode:
            return decision
        return PacingDecision(
            silence=decision.silence,
            mode=mode,
            reason=answer.get("reason", "") or decision.reason,
            score=decision.score,
            signals=decision.signals,
            decided_by="producer",
        )

    def judge_all(self, contexts: Sequence[SilenceContext]) -> list[PacingDecision]:
        return [self.judge(c) for c in contexts]


def trim_target(decision: PacingDecision, profile: CalibrationProfile) -> tuple[float, float] | None:
    """For a TRIM, the sub-span to remove so the configured breath survives."""
    if decision.mode is not PacingMode.TRIM:
        return decision.trimmed_span
    keep = profile.get_float("pacing.trim_target_sec")
    ctx = decision.silence
    if ctx.duration <= keep:
        return None
    # Keep the head of the silence (the beat that belongs to what just happened)
    # and drop the tail.
    return (ctx.start_sec + keep, ctx.end_sec)
