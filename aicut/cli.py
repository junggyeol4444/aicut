"""Command line interface.

Mirrors the operating flow of 15.1 - submit a source, watch the analysis, review
the discovered candidates, review and publish the episodes - and exposes the
learning loops and the calibration procedure as their own commands, because they
are run on their own schedule rather than per broadcast.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from aicut.config import CalibrationProfile
from aicut.db.store import Store
from aicut.errors import AicutError
from aicut.intelligence.knowledge import ProductionKnowledge
from aicut.llm import get_producer
from aicut.media.ffmpeg_util import have_ffmpeg
from aicut.media.stt import TranscriptFileTranscriber
from aicut.pipeline.context import RunContext
from aicut.pipeline.runner import Pipeline
from aicut.pipeline.states import State
from aicut.pipeline import review as review_mod
from aicut.render.editplan import EditPlan, describe

DEFAULT_WORKSPACE = Path("workspace")

# The parameters worth sweeping first: the ones 17.3 actually scores, and the
# ones a channel's own material moves most. Override with --grid.
DEFAULT_SWEEP_GRID = {
    "silence.level_db": [-45.0, -40.0, -35.0, -30.0],
    "pacing.keep_score_threshold": [0.3, 0.4, 0.5, 0.6],
    "pacing.keep_max_sec": [2.5, 3.5, 4.5],
    "pacing.cut_min_sec": [2.0, 2.5, 3.5],
    "tension.high": [0.55, 0.62, 0.7],
}


# ---------------------------------------------------------------------------
def _store(args) -> Store:
    return Store(Path(args.workspace) / "aicut.db")


def _profile(args) -> CalibrationProfile:
    return CalibrationProfile.load(args.profile, strict=getattr(args, "strict", False))


def _pipeline(args) -> Pipeline:
    knowledge_path = Path(args.workspace) / "knowledge.json"
    knowledge = ProductionKnowledge.load(knowledge_path).summary_for_planner()
    return Pipeline(
        _store(args),
        _profile(args),
        get_producer(args.producer),
        workspace=Path(args.workspace),
        knowledge=knowledge,
    )


def _context(args, project) -> RunContext:
    store = _store(args)
    return RunContext(
        project=project,
        store=store,
        profile=_profile(args),
        producer=get_producer(args.producer),
        workspace=Path(args.workspace),
    )


def _print(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
def cmd_run(args) -> int:
    pipeline = _pipeline(args)
    project = pipeline.submit(
        args.source, length_hint_sec=args.length_hint, channel_ref=args.channel or ""
    )
    transcriber = TranscriptFileTranscriber(args.transcript) if args.transcript else None
    if transcriber is None and not args.no_stt:
        transcriber = _whisperx(args)

    result = pipeline.run(
        project,
        transcriber=transcriber,
        stop_after=State(args.stop_after) if args.stop_after else None,
        sample_frames=args.frames,
        render=not args.no_render,
    )

    print(f"\nproject {result.project_id} -> {result.final_state.value}")
    if result.produced_nothing:
        print(f"  nothing worth producing: {result.report.get('no_content_reason')}")
        print("  (this is a normal outcome, not a failure - 16장)")
    for episode in result.report.get("episodes", []):
        print(
            f"  [{episode['target_type'] or '?'}] {episode['duration_sec']}s, {episode['cuts']} cuts,"
            f" structure={episode['structure']}"
        )
        if episode["titles"]:
            print(f"      title candidates: {' | '.join(episode['titles'])}")
        if episode["output"]:
            print(f"      output: {episode['output']}")
    if result.report.get("provisional_parameters_used"):
        print(f"\n  {result.report['warning']}")
        print(f"  provisional: {', '.join(result.report['provisional_parameters_used'])}")
    print(f"\n  report: {Path(args.workspace) / result.project_id / 'report.json'}")
    return 0 if result.final_state is not State.FAILED else 1


def cmd_resume(args) -> int:
    """Continue a project that stopped part way, without re-deciding what it knows."""
    pipeline = _pipeline(args)
    project = pipeline.store.get_project(args.project)
    if project is None:
        print(f"unknown project {args.project}", file=sys.stderr)
        return 1

    windows = len(pipeline.store.windows(args.project))
    events = len(pipeline.store.events(args.project))
    print(f"resuming {args.project} from {project.status}")
    if windows and events:
        print(f"  reusing {windows} window summaries and {events} events already understood")
    else:
        print("  nothing understood yet; this will read the broadcast from the start")

    result = pipeline.resume(
        args.project,
        transcriber=TranscriptFileTranscriber(args.transcript) if args.transcript else None,
        render=not args.no_render,
    )
    print(f"\n{args.project} -> {result.final_state.value}")
    for episode in result.report.get("episodes", []):
        print(f"  [{episode['target_type'] or '?'}] {episode['duration_sec']}s, {episode['cuts']} cuts")
    return 0 if result.final_state is not State.FAILED else 1


def cmd_status(args) -> int:
    store = _store(args)
    if args.project:
        project = store.get_project(args.project)
        if project is None:
            print(f"unknown project {args.project}", file=sys.stderr)
            return 1
        _print({
            "project": project.__dict__,
            "state_log": store.state_log(project.project_id),
            "episodes": [
                {"episode_id": e.episode_id, "render": e.render_status, "review": e.review_status,
                 "titles": e.title_candidates}
                for e in store.episodes(project.project_id)
            ],
        })
        return 0
    for project in store.list_projects():
        print(f"{project.project_id}  {project.status:<15} {project.file_path}")
    return 0


def cmd_candidates(args) -> int:
    """15.4: the candidate review screen, with the reasoning behind each decision."""
    store = _store(args)
    project = store.get_project(args.project)
    if project is None:
        print(f"unknown project {args.project}", file=sys.stderr)
        return 1
    ctx = _context(args, project)
    if args.verdict:
        review_mod.record_candidate_verdict(ctx, args.candidate, args.verdict, args.note or "")
        print(f"recorded '{args.verdict}' for {args.candidate}")
        _print(review_mod.agreement_rate(ctx))
        return 0
    for row in review_mod.candidate_review(ctx):
        mark = {"produce": "+", "combine": "~", "hold": "?", "reject": "-"}.get(row["decision"], " ")
        print(f"{mark} {row['candidate_id'][:8]}  {row['decision']:<8} {row['core_summary'][:70]}")
        print(f"    why: {row['reason']}")
        print(
            f"    independence={row['independence_score']:.2f} density={row['density_score']:.2f}"
            f" resolution={'yes' if row['has_resolution'] else 'no'}"
            + (f" human={row['human_verdict']}" if row["human_verdict"] else "")
        )
    _print(review_mod.agreement_rate(ctx))
    return 0


def cmd_plan(args) -> int:
    """Read an edit plan the way MVP 5's success test asks a person to."""
    print(describe(EditPlan.load(args.plan)))
    return 0


def cmd_render(args) -> int:
    """Re-run only the render, from a plan that survived a failure (16장)."""
    from aicut.pipeline import rendering

    store = _store(args)
    plan = EditPlan.load(args.plan)
    episode = store.get_episode(plan.episode_id)
    if episode is None:
        print(f"plan references unknown episode {plan.episode_id}", file=sys.stderr)
        return 1
    project = store.get_project(episode.project_id)
    ctx = _context(args, project)
    rendering.render_episode(ctx, episode, plan_path=args.plan)
    print(f"rendered {episode.output_mp4_path}")
    return 0


def cmd_review(args) -> int:
    """The mandatory gate of 11.3: nothing is published without passing here."""
    store = _store(args)
    episode = store.get_episode(args.episode)
    if episode is None:
        print(f"unknown episode {args.episode}", file=sys.stderr)
        return 1
    project = store.get_project(episode.project_id)
    ctx = _context(args, project)
    if args.action == "approve":
        review_mod.approve(ctx, args.episode, reviewer=args.reviewer, note=args.note or "")
        print(f"{args.episode} approved by {args.reviewer}; it may now be published")
    else:
        review_mod.reject(ctx, args.episode, reviewer=args.reviewer, reason=args.note or "")
        print(f"{args.episode} rejected by {args.reviewer}")
    return 0


def cmd_quota(args) -> int:
    from aicut.intelligence.quota import QuotaLedger

    store = _store(args)
    profile = _profile(args)
    ledger = QuotaLedger(
        store,
        daily_limit=profile.get_int("upload.daily_quota_units"),
        timezone_name=profile.get("upload.quota_reset_timezone"),
    )
    state = ledger.state()
    _print({
        "pt_date": state.pt_date,
        "used_units": state.used,
        "limit_units": state.limit,
        "remaining_units": state.remaining,
        "uploads_left_today": ledger.uploads_left_today(),
        "next_reset": ledger.next_reset().isoformat(),
        "queued_uploads": store.upload_queue(),
    })
    return 0


def cmd_calibrate(args) -> int:
    """17.4: measure a starting point, sweep, score, save the channel's profile."""
    from aicut.calibration import ReplayHarness, build_evaluator, sweep
    from aicut.calibration.dataset import Dataset

    if args.init:
        return _calibrate_init(args)

    if not args.dataset:
        print("a sweep needs --dataset (see `aicut dataset init`), or use --init", file=sys.stderr)
        return 1

    dataset = Dataset.load(args.dataset)
    base = _profile(args)
    grid = json.loads(Path(args.grid).read_text(encoding="utf-8")) if args.grid else DEFAULT_SWEEP_GRID
    if not args.grid:
        print(f"no --grid given; sweeping the default grid over {', '.join(grid)}")

    if args.harness:
        # An operator with their own replay keeps using it.
        from importlib import util

        spec = util.spec_from_file_location("aicut_eval_harness", args.harness)
        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)                      # type: ignore[union-attr]
        evaluate = lambda profile: module.run(profile, dataset.to_dict())    # noqa: E731
        harness = None
    else:
        harness = ReplayHarness(
            dataset, workspace=Path(args.workspace), project_id=args.project,
            producer=get_producer(args.producer),
        )
        evaluate = build_evaluator(harness)

    try:
        result = sweep(base, grid, evaluate, channel_ref=args.channel or dataset.channel_ref)
    finally:
        if harness is not None:
            harness.close()

    out = Path(args.out or Path(args.workspace) / "profiles" / f"{result.profile.name}.json")
    result.profile.save(out)
    result.save_trials(out.with_suffix(".trials.json"))
    _record_profile(args, result.profile)
    print(f"best score {result.best_score}: {result.best_params}")
    print(f"  {len(result.trials)} trials scored against {args.dataset}")
    print(f"  {', '.join(dataset.coverage()['ready_for'])}")
    print(f"profile saved to {out}")
    print(f"use it with: aicut --profile {out} run <source>")
    return 0


def _calibrate_init(args) -> int:
    """17.4 step 1: read a starting silence level off this channel's own audio.

    Rather than adopting somebody else's -40 dB, take the level distribution of
    a real broadcast from this setup and put the silence line low in it. Still a
    starting point for the sweep, not a measurement of what sounds silent.
    """
    from aicut.calibration.sweep import initial_estimates
    from aicut.pipeline.context import SignalBundle

    store = _store(args)
    project = store.get_project(args.project) if args.project else (store.list_projects() or [None])[-1]
    if project is None:
        print("no project to measure; run one first (its signals are cached)", file=sys.stderr)
        return 1
    cache = Path(args.workspace) / project.project_id / "signals.json"
    if not cache.exists():
        print(f"no cached signals for {project.project_id}", file=sys.stderr)
        return 1

    signals = SignalBundle.load(cache)
    if not signals.rms:
        print("the cached signals hold no RMS envelope to measure", file=sys.stderr)
        return 1

    estimates = initial_estimates(level for _, level in signals.rms)
    profile = _profile(args).with_overrides(estimates, measured=estimates.keys())
    profile.name = args.channel or f"{profile.name}-init"
    out = Path(args.out or Path(args.workspace) / "profiles" / f"{profile.name}.json")
    profile.save(out)
    _record_profile(args, profile)
    print(f"measured from {project.file_path}: {estimates}")
    print(f"profile saved to {out}")
    print("this is step 1 of 17.4; run the sweep before treating these as final")
    return 0


def _record_profile(args, profile: CalibrationProfile) -> str:
    """Keep the profile in TB_CALIBRATION_PROFILE too (13장), not only as a file.

    22.7 lists the channel profile as a deliverable, and 17.4 says a profile is
    re-measured whenever the setup changes - so the history of what was measured
    when has to live somewhere queryable, not only in whichever file was written
    last.
    """
    return _store(args).save_profile(
        name=profile.name,
        channel_ref=args.channel or "",
        params=profile.to_mapping(),
        measured_at=profile.measured_at,
        eval_score=profile.eval_score,
    )


def cmd_profile(args) -> int:
    if args.list:
        _print([
            {"profile_id": row["profile_id"], "name": row["name"], "channel_ref": row["channel_ref"],
             "measured_at": row["measured_at"], "eval_score": row["eval_score"]}
            for row in _store(args).profiles()
        ])
        return 0
    profile = _profile(args)
    _print({
        "name": profile.name,
        "source": str(profile.source_path),
        "measured_at": profile.measured_at,
        "eval_score": profile.eval_score,
        "provisional_parameters": sorted(profile.provisional),
        "measured_parameters": sorted(profile.measured),
        "notes": profile.notes,
    })
    return 0


def cmd_learn(args) -> int:
    """Run one of the three learning loops (12.3)."""
    from aicut.intelligence import reference as reference_mod
    from aicut.intelligence.knowledge import ProductionKnowledge

    store = _store(args)
    producer = get_producer(args.producer)
    knowledge_path = Path(args.workspace) / "knowledge.json"

    if args.loop == "reference":
        # Loop A. Only public metrics are fetched, and only patterns are kept (4.2, 4.6).
        client = _youtube(args, store)
        queries = args.query or reference_mod.DEFAULT_QUERIES
        references = reference_mod.collect_references(client, queries, per_query=args.per_query)
        print(f"collected {len(references)} references; analysing")
        reference_mod.analyze(producer, store, references)
        knowledge = reference_mod.build_knowledge(store)
        knowledge.save(knowledge_path)
        print(f"knowledge from {knowledge.sample_size} references -> {knowledge_path}")
        return 0

    if args.loop == "pairs":
        # Loop B, the differentiator: what a human actually kept, dropped, reordered.
        from aicut.intelligence.source_output import align_by_transcript, learn as learn_pair
        from aicut.media.stt import TranscriptFileTranscriber

        if not args.source_transcript or not args.output_transcript:
            print("loop B needs --source-transcript and --output-transcript", file=sys.stderr)
            return 1
        source = TranscriptFileTranscriber(args.source_transcript).transcribe()
        output = TranscriptFileTranscriber(args.output_transcript).transcribe()
        alignment = align_by_transcript(source, output)
        analysis = learn_pair(
            producer, store, alignment,
            source_ref=args.source_ref or args.source_transcript,
            output_ref=args.output_ref or args.output_transcript,
        )
        measured = analysis["measured"]
        print(
            f"kept {measured['kept_spans']} spans, dropped {measured['dropped_spans']},"
            f" keep_ratio {measured['keep_ratio']}, reordered {measured['reordered']}"
        )
        for rule in analysis.get("inferred_rules", []):
            print(f"  rule: {rule}")
        knowledge = ProductionKnowledge.load(knowledge_path)
        knowledge.source_output_rules.extend(analysis.get("inferred_rules", []))
        knowledge.save(knowledge_path)
        print(f"this pair also serves as a 17.2 calibration dataset entry")
        return 0

    # Loop C.
    from aicut.pipeline import performance

    project = store.get_project(args.project)
    if project is None:
        print(f"unknown project {args.project}", file=sys.stderr)
        return 1
    ctx = _context(args, project)
    client = _youtube(args, store)
    collected = performance.collect(ctx, client, days=args.days)
    print(f"collected metrics for {len(collected)} published episodes")
    result = performance.learn(ctx, knowledge_path)
    for observation in result.get("observations", []):
        print(f"  {observation}")
    return 0


def cmd_upload(args) -> int:
    """Upload rendered episodes privately, or drain the retry queue (11.3, 11.4)."""
    from aicut.intelligence.quota import QuotaLedger
    from aicut.pipeline import publishing

    store = _store(args)
    profile = _profile(args)
    ledger = QuotaLedger(
        store,
        daily_limit=profile.get_int("upload.daily_quota_units"),
        timezone_name=profile.get("upload.quota_reset_timezone"),
    )
    client = _youtube(args, store, ledger=ledger)

    if args.retry:
        project = store.get_project(args.project) if args.project else None
        if project is None:
            projects = store.list_projects()
            if not projects:
                print("no projects", file=sys.stderr)
                return 1
            project = projects[-1]
        ctx = _context(args, project)
        done = publishing.process_retry_queue(ctx, client, ledger)
        print(f"uploaded {len(done)} queued episodes; {ledger.uploads_left_today()} uploads left today")
        return 0

    episode = store.get_episode(args.episode)
    if episode is None:
        print(f"unknown episode {args.episode}", file=sys.stderr)
        return 1
    ctx = _context(args, store.get_project(episode.project_id))
    if args.publish:
        publishing.publish_approved(ctx, episode, client)
        print(f"{episode.episode_id} is now public")
        return 0
    result = publishing.upload_episode(ctx, episode, client)
    print(f"uploaded {result['url']} as {result['privacy_status']}")
    print("it stays private until a person approves it: aicut review <episode> approve --reviewer <name>")
    return 0


def _youtube(args, store, ledger=None):
    from aicut.intelligence.quota import QuotaLedger
    from aicut.intelligence.youtube import YouTubeClient, load_credentials

    profile = _profile(args)
    ledger = ledger or QuotaLedger(
        store,
        daily_limit=profile.get_int("upload.daily_quota_units"),
        timezone_name=profile.get("upload.quota_reset_timezone"),
    )
    credentials = load_credentials(
        args.client_secrets, args.token or str(Path(args.workspace) / "youtube_token.json")
    )
    return YouTubeClient(credentials, ledger)


def cmd_ui(args) -> int:
    """The operator screens of 15.1, served on localhost."""
    from aicut.ui import serve

    httpd, ui = serve(
        Path(args.workspace), host=args.host, port=args.port,
        profile_path=args.profile, producer_name=args.producer,
    )
    print(f"aicut ui on http://{args.host}:{args.port}  (workspace {args.workspace})")
    print("localhost only, no authentication - do not expose this port")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
        ui.close()
    return 0


def cmd_transcribe(args) -> int:
    """Produce a transcript and stop (20.2).

    STT is the one stage that wants a GPU, and it does not have to run where the
    editing runs. Splitting it out means the transcript can be made on whatever
    machine has the hardware and carried to the one that does the work - which
    is also how the transcript for a source/output pair gets made (17.2).
    """
    from aicut.media.probe import probe
    from aicut.media.stt import write_transcript

    media = probe(args.source)
    media.validate()
    transcriber = _transcriber(args)
    print(f"transcribing {args.source} ({media.duration_sec / 60:.1f} min) with {args.backend}")

    import time

    started = time.time()
    utterances = transcriber.transcribe(args.source, media)
    elapsed = time.time() - started

    out = Path(args.out or Path(args.source).with_suffix(".transcript.json"))
    write_transcript(utterances, out)
    words = sum(len(u.words) for u in utterances)
    factor = media.duration_sec / elapsed if elapsed else 0.0
    print(f"  {len(utterances)} segments, {words} word timings, {elapsed:.1f}s ({factor:.1f}x realtime)")
    if not words:
        print("  WARNING: no word timings came out; pacing measures gaps between words and"
              " subtitle timing depends on them")
    print(f"  written to {out}")
    return 0


def _transcriber(args):
    from aicut.media.stt import FasterWhisperTranscriber, WhisperXTranscriber

    if args.backend == "faster-whisper":
        return FasterWhisperTranscriber(
            model_size=args.stt_model, device=args.device,
            compute_type=args.compute_type, language=args.language,
        )
    return WhisperXTranscriber(
        model_size=args.stt_model, device=args.device, language=args.language,
        hf_token=args.hf_token, diarize=not args.no_diarize,
    )


def cmd_dataset(args) -> int:
    """Build the labelled dataset of 17.2 - the thing every threshold waits on."""
    from aicut.calibration.dataset import Dataset

    path = Path(args.file)

    if args.action == "init":
        if path.exists() and not args.force:
            print(f"{path} already exists; pass --force to start over", file=sys.stderr)
            return 1
        dataset = Dataset(
            source_path=args.source or "",
            transcript_path=args.transcript,
            output_path=args.output,
            channel_ref=args.channel or "",
        )
        if not dataset.source_path:
            print("a dataset must name its source broadcast: --source <file>", file=sys.stderr)
            return 1
        dataset.save(path)
        print(f"dataset created at {path}")
        print("next: mark the stretches a person would make a video out of, with")
        print(f"  aicut dataset add-content {path} --start 01:12:30 --end 01:19:05 --note '...'")
        return 0

    dataset = Dataset.load(path)

    if args.action == "add-content":
        span = dataset.add_content(_seconds(args.start), _seconds(args.end), args.note or "")
        dataset.save(path)
        print(f"content span {_hms(span.start_sec)}-{_hms(span.end_sec)} added"
              f" ({len(dataset.content_spans)} total)")
        return 0

    if args.action == "add-silence":
        verdict = dataset.add_silence_verdict(
            _seconds(args.start), _seconds(args.end), kept=args.kept, note=args.note or ""
        )
        dataset.save(path)
        print(f"silence {_hms(verdict.start_sec)}-{_hms(verdict.end_sec)}"
              f" marked {'kept' if verdict.kept else 'cut'}")
        return 0

    if args.action == "derive-silences":
        return _derive_silences(args, dataset, path)

    _print({"dataset": dataset.to_dict(), "coverage": dataset.coverage()})
    return 0


def _derive_silences(args, dataset, path: Path) -> int:
    """Read the pacing labels out of the human edit instead of typing them (12.3 B, 17.2)."""
    from aicut.intelligence.source_output import align_by_transcript
    from aicut.media.stt import TranscriptFileTranscriber
    from aicut.pipeline.context import SignalBundle

    if not dataset.transcript_path or not args.output_transcript:
        print("deriving needs the source transcript in the dataset and --output-transcript",
              file=sys.stderr)
        return 1

    store = _store(args)
    source_resolved = str(Path(dataset.source_path).resolve())
    project = next(
        (p for p in reversed(store.list_projects())
         if str(Path(p.file_path).resolve()) == source_resolved),
        None,
    )
    if project is None:
        print(f"run {dataset.source_path} once first so its silences are measured", file=sys.stderr)
        return 1

    cache = Path(args.workspace) / project.project_id / "signals.json"
    if not cache.exists():
        print(f"no cached signals for {project.project_id}", file=sys.stderr)
        return 1

    signals = SignalBundle.load(cache)
    alignment = align_by_transcript(
        TranscriptFileTranscriber(dataset.transcript_path).transcribe(),
        TranscriptFileTranscriber(args.output_transcript).transcribe(),
    )
    verdicts = dataset.derive_silence_verdicts(signals.silences, alignment)
    dataset.save(path)
    kept = sum(1 for v in verdicts if v.kept)
    print(f"derived {len(verdicts)} silence verdicts from the human edit: {kept} kept, {len(verdicts) - kept} cut")
    print(f"  keep ratio of the edit itself: {alignment.keep_ratio:.2f}")
    return 0


def _seconds(value: str) -> float:
    """Accept 91.5, 1:31.5 or 01:12:30."""
    parts = str(value).split(":")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return total


def _hms(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def cmd_benchmark(args) -> int:
    """Measure this machine against a real source (R3, 20.2).

    R3 names processing time as an open risk and 20.2 lists measuring it as a
    prerequisite, so the measurement is a command rather than a paragraph. What
    it reports is the signal-extraction cost - the part that scales with source
    length and runs on every project - and what that extrapolates to for a
    six-hour broadcast on this hardware.
    """
    import time

    from aicut.media import audio as audio_mod
    from aicut.media import vision as vision_mod
    from aicut.media.probe import probe

    profile = _profile(args)
    media = probe(args.source)
    media.validate()
    print(f"source: {args.source}")
    print(f"  {media.duration_sec / 60:.1f} min, {media.width}x{media.height},"
          f" {len(media.audio_tracks)} audio track(s)")

    steps: dict[str, float] = {}

    def timed(label: str, fn):
        started = time.time()
        result = fn()
        steps[label] = time.time() - started
        print(f"  {label:22s} {steps[label]:7.2f}s")
        return result

    silences = timed("silence detection", lambda: audio_mod.detect_silences(args.source, profile))
    rms = timed("loudness envelope", lambda: audio_mod.rms_envelope(args.source))
    motion = timed("visual change", lambda: vision_mod.motion_curve(
        args.source, interval_sec=profile.get_float("scan.pass1_frame_interval_sec")))

    if args.frames:
        frames = timed("frame sampling", lambda: vision_mod.sample_frames(
            args.source, Path(args.workspace) / "benchmark_frames",
            start_sec=0, duration_sec=media.duration_sec,
            interval_sec=profile.get_float("scan.pass1_frame_interval_sec")))
        from aicut.media import faces as faces_mod

        detector = faces_mod.build_detector()
        if detector is not None:
            timed("face detection", lambda: detector.read_frames([(f.at_sec, f.path) for f in frames]))
        else:
            print("  face detection         skipped (no detector available)")

    total = sum(steps.values())
    factor = media.duration_sec / total if total else 0.0
    print()
    print(f"  measured {len(silences)} silences, {len(rms)} loudness frames, {len(motion)} motion samples")
    print(f"  total {total:.1f}s for {media.duration_sec / 60:.1f} min of source ({factor:.1f}x realtime)")
    if factor:
        print(f"  a six-hour broadcast would take about {6 * 3600 / factor / 60:.1f} min of signal extraction"
              " on this machine")
    print("  STT and the reasoning passes are extra and dominate on a real run;"
          " measure those on the hardware that will run them (20.2).")
    return 0


def cmd_doctor(args) -> int:
    """Check the preconditions of 20.2 before a run rather than during one."""
    checks = {
        "ffmpeg/ffprobe on PATH": have_ffmpeg(),
        "whisperx installed": _importable("whisperx"),
        "pyannote installed (gated model, needs HF approval - 20.2)": _importable("pyannote.audio"),
        "anthropic sdk installed": _importable("anthropic"),
        "google api client installed": _importable("googleapiclient"),
        "opencv installed": _importable("cv2"),
    }
    for name, ok in checks.items():
        print(f"  [{'ok' if ok else '--'}] {name}")
    profile = _profile(args)
    if profile.provisional:
        print(
            f"\n  {len(profile.provisional)} parameter groups are still unmeasured guesses in"
            f" profile '{profile.name}' (17.5). Run `aicut calibrate` before relying on output."
        )
    return 0


def _importable(name: str) -> bool:
    from importlib import util

    try:
        return util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _whisperx(args):
    return _transcriber(args)


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aicut", description=__doc__)
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="where outputs and the database live")
    parser.add_argument("--profile", default=None, help="calibration profile json (17장)")
    parser.add_argument("--producer", default="mock", choices=["mock", "anthropic"], help="reasoning backend")
    parser.add_argument("--strict", action="store_true", help="refuse to read provisional parameters (17.5)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="process one broadcast end to end")
    run.add_argument("source", help="the livestream file (.mp4/.mkv)")
    run.add_argument("--transcript", help="use an existing WhisperX-shaped transcript instead of running STT")
    run.add_argument("--no-stt", action="store_true", help="skip STT entirely (uses stored utterances)")
    run.add_argument("--length-hint", type=float, default=None, help="target length hint in seconds (2.6: a hint)")
    run.add_argument("--channel", default=None)
    run.add_argument("--stop-after", choices=[s.value for s in State], default=None)
    run.add_argument("--no-render", action="store_true", help="stop after the edit plan (MVP 5)")
    run.add_argument("--frames", action="store_true", help="sample frames for the visual half of each pass")
    run.add_argument("--backend", choices=["faster-whisper", "whisperx"], default="whisperx")
    run.add_argument("--stt-model", default="large-v3")
    run.add_argument("--compute-type", default="int8")
    run.add_argument("--language", default=None)
    run.add_argument("--device", default="cuda")
    run.add_argument("--hf-token", default=None)
    run.add_argument("--no-diarize", action="store_true")
    run.set_defaults(func=cmd_run)

    resume = sub.add_parser("resume", help="continue a project that stopped part way (16장)")
    resume.add_argument("project")
    resume.add_argument("--transcript")
    resume.add_argument("--no-render", action="store_true")
    resume.set_defaults(func=cmd_resume)

    status = sub.add_parser("status", help="list projects or show one")
    status.add_argument("project", nargs="?")
    status.set_defaults(func=cmd_status)

    candidates = sub.add_parser("candidates", help="review discovered candidates (15.4)")
    candidates.add_argument("project")
    candidates.add_argument("--candidate")
    candidates.add_argument("--verdict", choices=["agree", "disagree"])
    candidates.add_argument("--note")
    candidates.set_defaults(func=cmd_candidates)

    plan = sub.add_parser("plan", help="print an edit plan in human form")
    plan.add_argument("plan")
    plan.set_defaults(func=cmd_plan)

    render = sub.add_parser("render", help="render (or re-render) from an edit plan")
    render.add_argument("plan")
    render.set_defaults(func=cmd_render)

    review = sub.add_parser("review", help="approve or reject an episode (11.3 gate)")
    review.add_argument("episode")
    review.add_argument("action", choices=["approve", "reject"])
    review.add_argument("--reviewer", required=True)
    review.add_argument("--note")
    review.set_defaults(func=cmd_review)

    quota = sub.add_parser("quota", help="YouTube quota state and the next PT reset (11.4)")
    quota.set_defaults(func=cmd_quota)

    calibrate = sub.add_parser("calibrate", help="sweep parameters against a labelled dataset (17.4)")
    calibrate.add_argument("--init", action="store_true",
                           help="17.4 step 1: measure starting values from a processed broadcast")
    calibrate.add_argument("--project", help="which project's cached signals to measure (--init)")
    calibrate.add_argument("--dataset", help="17.2 dataset json (see `aicut dataset`)")
    calibrate.add_argument("--grid", help="json of dotted parameter path -> values (default: a built-in grid)")
    calibrate.add_argument("--harness", help="optional: your own python file exposing run(profile, dataset)")
    calibrate.add_argument("--channel")
    calibrate.add_argument("--out")
    calibrate.set_defaults(func=cmd_calibrate)

    prof = sub.add_parser("profile", help="show a calibration profile and what is still a guess")
    prof.add_argument("--list", action="store_true", help="list the profiles measured so far (13장)")
    prof.set_defaults(func=cmd_profile)

    learn = sub.add_parser("learn", help="run a learning loop (12.3)")
    learn.add_argument("loop", choices=["reference", "pairs", "performance"])
    learn.add_argument("--query", action="append", help="reference search query (loop A, repeatable)")
    learn.add_argument("--per-query", type=int, default=25)
    learn.add_argument("--source-transcript", help="loop B: transcript of the source broadcast")
    learn.add_argument("--output-transcript", help="loop B: transcript of the human-made video")
    learn.add_argument("--source-ref")
    learn.add_argument("--output-ref")
    learn.add_argument("--project", help="loop C: which project's published episodes")
    learn.add_argument("--days", type=int, default=28)
    learn.add_argument("--client-secrets", default="client_secrets.json")
    learn.add_argument("--token")
    learn.set_defaults(func=cmd_learn)

    upload = sub.add_parser("upload", help="upload privately, publish an approved episode, or retry (11.3, 11.4)")
    upload.add_argument("episode", nargs="?")
    upload.add_argument("--publish", action="store_true", help="make an approved episode public")
    upload.add_argument("--retry", action="store_true", help="drain the quota retry queue")
    upload.add_argument("--project")
    upload.add_argument("--client-secrets", default="client_secrets.json")
    upload.add_argument("--token")
    upload.set_defaults(func=cmd_upload)

    ui = sub.add_parser("ui", help="operator screens: submit, monitor, review (15장)")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8765)
    ui.set_defaults(func=cmd_ui)

    transcribe = sub.add_parser("transcribe", help="run STT and write a transcript (20.2)")
    transcribe.add_argument("source")
    transcribe.add_argument("-o", "--out")
    transcribe.add_argument("--backend", choices=["faster-whisper", "whisperx"], default="faster-whisper",
                            help="faster-whisper runs on a CPU; whisperx wants a GPU")
    transcribe.add_argument("--stt-model", default="base")
    transcribe.add_argument("--device", default="cpu")
    transcribe.add_argument("--compute-type", default="int8")
    transcribe.add_argument("--language", default=None)
    transcribe.add_argument("--hf-token", default=None)
    transcribe.add_argument("--no-diarize", action="store_true")
    transcribe.set_defaults(func=cmd_transcribe)

    dataset = sub.add_parser("dataset", help="build the labelled calibration dataset (17.2)")
    dataset.add_argument("action", choices=["init", "add-content", "add-silence", "derive-silences", "show"])
    dataset.add_argument("file")
    dataset.add_argument("--source", help="the broadcast this dataset labels")
    dataset.add_argument("--transcript")
    dataset.add_argument("--output", help="the video a human cut from it (17.2 b)")
    dataset.add_argument("--output-transcript", help="its transcript, for derive-silences")
    dataset.add_argument("--channel")
    dataset.add_argument("--start", help="timestamp: 91.5, 1:31.5 or 01:12:30")
    dataset.add_argument("--end")
    dataset.add_argument("--note")
    dataset.add_argument("--kept", action="store_true", help="add-silence: the human kept this pause")
    dataset.add_argument("--force", action="store_true")
    dataset.set_defaults(func=cmd_dataset)

    benchmark = sub.add_parser("benchmark", help="measure signal extraction on this machine (R3, 20.2)")
    benchmark.add_argument("source")
    benchmark.add_argument("--frames", action="store_true",
                           help="also time frame sampling and face detection")
    benchmark.set_defaults(func=cmd_benchmark)

    doctor = sub.add_parser("doctor", help="check the prerequisites of 20.2")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def _force_utf8_console() -> None:
    """Print Korean paths and titles without dying on the console codepage.

    A Windows console defaults to the system codepage (cp949 for a Korean
    install), and printing a path this tool routinely handles -
    방송_2026-08-19.mkv - raises UnicodeEncodeError there. Reconfiguring the
    streams costs nothing on POSIX, where they are already UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):          # a redirected or closed stream
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except AicutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
