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
    """17.4: sweep the parameters against a source/output dataset and save a profile."""
    from aicut.calibration import sweep
    from aicut.calibration.metrics import combined_score, score_content_discovery, score_pacing

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    base = _profile(args)
    grid = json.loads(Path(args.grid).read_text(encoding="utf-8"))

    def evaluate(profile: CalibrationProfile) -> float:
        # The dataset supplies human verdicts; the harness that replays this
        # profile over the source lives in the project's own eval script and is
        # imported here by path so the metric stays the shared part.
        from importlib import util

        spec = util.spec_from_file_location("aicut_eval_harness", args.harness)
        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)                      # type: ignore[union-attr]
        system = module.run(profile, dataset)
        pacing = score_pacing(system["pacing_keeps"], dataset["pacing_keeps"]) if "pacing_keeps" in dataset else None
        discovery = (
            score_content_discovery(
                [tuple(s) for s in system.get("content_spans", [])],
                [tuple(s) for s in dataset.get("content_spans", [])],
            )
            if "content_spans" in dataset else None
        )
        return combined_score(pacing, discovery)

    result = sweep(base, grid, evaluate, channel_ref=args.channel or "")
    out = Path(args.out or Path(args.workspace) / "profiles" / f"{result.profile.name}.json")
    result.profile.save(out)
    result.save_trials(out.with_suffix(".trials.json"))
    print(f"best score {result.best_score}: {result.best_params}")
    print(f"profile saved to {out}")
    return 0


def cmd_profile(args) -> int:
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
    from aicut.media.stt import WhisperXTranscriber

    return WhisperXTranscriber(
        model_size=args.stt_model, device=args.device, hf_token=args.hf_token, diarize=not args.no_diarize
    )


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
    run.add_argument("--stt-model", default="large-v3")
    run.add_argument("--device", default="cuda")
    run.add_argument("--hf-token", default=None)
    run.add_argument("--no-diarize", action="store_true")
    run.set_defaults(func=cmd_run)

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
    calibrate.add_argument("--dataset", required=True, help="17.2 dataset json")
    calibrate.add_argument("--grid", required=True, help="json of dotted parameter path -> values to try")
    calibrate.add_argument("--harness", required=True, help="python file exposing run(profile, dataset)")
    calibrate.add_argument("--channel")
    calibrate.add_argument("--out")
    calibrate.set_defaults(func=cmd_calibrate)

    prof = sub.add_parser("profile", help="show a calibration profile and what is still a guess")
    prof.set_defaults(func=cmd_profile)

    doctor = sub.add_parser("doctor", help="check the prerequisites of 20.2")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
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
