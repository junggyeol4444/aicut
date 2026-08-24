"""PARSING: get the measurable facts out of the media (5.2, program side).

Speech, silence, loudness and visual change are extracted once and cached. The
multi-track assumption of 5.2/20장 is honoured here: speech is read from the
speech tracks, so diarisation is only ever asked for on a track that really
carries more than one person.
"""

from __future__ import annotations

import logging

from aicut.analysis.tension import build_tension_curve
from aicut.media import audio as audio_mod
from aicut.media import vision as vision_mod
from aicut.media.probe import probe
from aicut.media.stt import Transcriber, speaker_reliability
from aicut.pipeline.context import RunContext, SignalBundle

log = logging.getLogger(__name__)


def run(ctx: RunContext, transcriber: Transcriber | None = None, *, use_cache: bool = True) -> RunContext:
    # A caller that already probed the media (a resumed run, a test fixture, a
    # GPU box that did the measuring elsewhere) passes it in and skips decoding.
    if ctx.media is None:
        ctx.media = probe(ctx.project.file_path)
    if ctx.media.duration_sec:
        ctx.project.duration_sec = ctx.media.duration_sec
        ctx.store.set_duration(ctx.project.project_id, ctx.media.duration_sec)

    if use_cache and ctx.signal_cache_path.exists():
        ctx.signals = SignalBundle.load(ctx.signal_cache_path)
        log.info("reusing cached signals from %s", ctx.signal_cache_path)
    else:
        speech_track = ctx.media.track_by_role("mic") or (ctx.media.audio_tracks[0] if ctx.media.audio_tracks else None)
        track_index = speech_track.index if speech_track and ctx.media.is_multitrack else None

        silences = audio_mod.detect_silences(ctx.project.file_path, ctx.profile, track_index=track_index)
        rms = audio_mod.rms_envelope(ctx.project.file_path, track_index=track_index)
        motion = vision_mod.motion_curve(
            ctx.project.file_path,
            interval_sec=ctx.profile.get_float("scan.pass1_frame_interval_sec"),
        )
        ctx.signals = SignalBundle(motion=motion, silences=silences, rms=rms)

    utterances = []
    if transcriber is not None:
        utterances = transcriber.transcribe(ctx.project.file_path, ctx.media)
        ctx.store.replace_utterances(ctx.project.project_id, utterances)
    else:
        utterances = ctx.store.utterances(ctx.project.project_id)

    # The tension curve is derived, not measured: rebuild it from the cached RMS
    # every run so a re-tuned profile takes effect without touching the media.
    ctx.signals.tension = build_tension_curve(ctx.signals.rms, utterances, ctx.profile)
    if utterances:
        ctx.signals.speaker_reliability = speaker_reliability(utterances)

    ctx.signals.save(ctx.signal_cache_path)
    ctx.note("media", {
        "duration_sec": ctx.media.duration_sec,
        "resolution": f"{ctx.media.width}x{ctx.media.height}",
        "audio_tracks": [{"index": t.index, "role": t.role, "title": t.title} for t in ctx.media.audio_tracks],
        "multitrack": ctx.media.is_multitrack,
    })
    ctx.note("utterance_count", len(utterances))
    ctx.note("speaker_reliability", round(ctx.signals.speaker_reliability, 3))
    if ctx.signals.speaker_reliability < 1.0:
        ctx.note(
            "speaker_note",
            "some speech carries no speaker tag; speaker-dependent staging is disabled for those parts (16장)",
        )
    return ctx
