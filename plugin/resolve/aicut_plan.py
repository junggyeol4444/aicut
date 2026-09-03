"""Turn an aicut edit plan into the clip list DaVinci Resolve wants.

This module holds every decision the plugin makes and touches nothing in
Resolve, so it can be tested on a machine that does not have Resolve on it -
which is the machine this was written on. `aicut_resolve.py` is the part that
talks to the application, and it is deliberately thin: if something is wrong
with the timeline, the arithmetic is here and it has tests.

Kept to the standard library and to Python 3.6 syntax, because it runs inside
Resolve's own interpreter, not in the aicut virtualenv.
"""

import json
import os

#: Resolve's `endFrame` is the LAST frame of the clip, not one past it. Off by
#: one here means every cut in the timeline is a frame long or a frame short,
#: which nobody notices until the export.
END_FRAME_IS_INCLUSIVE = True


class PlanError(Exception):
    """The plan cannot be turned into a timeline."""


def load_plan(path):
    """Read an aicut edit plan, with the errors a person can act on."""
    if not os.path.exists(path):
        raise PlanError("no edit plan at {}".format(path))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            plan = json.load(handle)
    except ValueError as exc:
        raise PlanError("{} is not valid JSON: {}".format(path, exc))
    if "cuts" not in plan:
        raise PlanError(
            "{} has no 'cuts'; this is not an aicut edit plan. Plans are written "
            "to <workspace>/<project>/plans/.".format(path)
        )
    if not plan["cuts"]:
        raise PlanError("this plan has no cuts in it - there is no timeline to build")
    return plan


def source_path(plan):
    path = plan.get("source_path") or ""
    if not path:
        raise PlanError("the plan does not say which file it was cut from")
    return path


def kept_spans(cut):
    """The parts of one cut that survive pacing, in source seconds.

    Mirrors `Cut.kept_spans` in aicut: a cut carries spans the renderer must
    drop from inside it (9.3), and a timeline that ignores them plays the dead
    air the plan decided to remove.
    """
    spans = [(float(cut["source_start_sec"]), float(cut["source_end_sec"]))]
    for removal in sorted(tuple(r) for r in cut.get("remove_spans", []) or []):
        start, end = float(removal[0]), float(removal[1])
        out = []
        for a, b in spans:
            if end <= a or start >= b:
                out.append((a, b))
                continue
            if start > a:
                out.append((a, min(start, b)))
            if end < b:
                out.append((max(end, a), b))
        spans = out
    return [(a, b) for a, b in spans if b - a > 1e-3]


def clip_list(plan, fps, media_pool_item=None):
    """The list Resolve's `CreateTimelineFromClips` takes, in plan order.

    Order is the plan's `sequence_order`, not source time: 2.4 lets a video
    open on a moment that happened last, and sorting by source would quietly
    undo the structure the plan chose.
    """
    if fps <= 0:
        raise PlanError("frame rate must be positive, got {}".format(fps))

    entries = []
    for cut in sorted(plan["cuts"], key=lambda c: c.get("sequence_order", 0)):
        for start_sec, end_sec in kept_spans(cut):
            start_frame = int(round(start_sec * fps))
            end_frame = int(round(end_sec * fps))
            if END_FRAME_IS_INCLUSIVE:
                end_frame -= 1
            if end_frame < start_frame:
                # Shorter than one frame at this rate. Dropping it silently
                # would shift everything after it, so it is reported instead.
                continue
            entry = {"startFrame": start_frame, "endFrame": end_frame}
            if media_pool_item is not None:
                entry["mediaPoolItem"] = media_pool_item
            entries.append(entry)
    if not entries:
        raise PlanError(
            "every cut in this plan is shorter than one frame at {} fps".format(fps)
        )
    return entries


def dropped_spans(plan, fps):
    """Spans too short to survive at this frame rate, so the caller can say so."""
    dropped = []
    for cut in sorted(plan["cuts"], key=lambda c: c.get("sequence_order", 0)):
        for start_sec, end_sec in kept_spans(cut):
            if int(round(end_sec * fps)) - 1 < int(round(start_sec * fps)):
                dropped.append((start_sec, end_sec))
    return dropped


def timeline_name(plan):
    """A name a person can find again, not a bare UUID."""
    episode = str(plan.get("episode_id", "episode"))
    target = plan.get("target_type") or "cut"
    return "aicut {} {}".format(target, episode[:8])


def subtitle_path(plan_path):
    """Where `aicut export --format srt` puts the subtitles for this plan."""
    base, _ = os.path.splitext(plan_path)
    candidate = base + ".srt"
    return candidate if os.path.exists(candidate) else None


def summary(plan, fps):
    """One paragraph a person can check the timeline against."""
    entries = clip_list(plan, fps)
    total_frames = sum(e["endFrame"] - e["startFrame"] + 1 for e in entries)
    return (
        "{} clips, {:.1f}s at {} fps, from {}".format(
            len(entries), total_frames / float(fps), fps, os.path.basename(source_path(plan))
        )
    )
