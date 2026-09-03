"""DaVinci Resolve plugin: build a timeline from an aicut edit plan.

Install by copying this folder into Resolve's script directory:

    Windows  %APPDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility
    macOS    ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility
    Linux    ~/.local/share/DaVinciResolve/Fusion/Scripts/Utility

Then, with a project open: Workspace > Scripts > aicut_resolve.

Every decision - which spans survive, what order they go in, how seconds
become frames - lives in `aicut_plan.py`, which has tests that run without
Resolve. This file is only the part that talks to the application, because
that part cannot be tested anywhere but on a machine with Resolve installed.

NOT VERIFIED HERE. Resolve is not present in the environment this was written
in, so these API calls are written against Blackmagic's scripting
documentation and have not been executed. The arithmetic they depend on has
been. Treat the first run as the test.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aicut_plan import (  # noqa: E402
    PlanError,
    clip_list,
    dropped_spans,
    load_plan,
    source_path,
    subtitle_path,
    summary,
    timeline_name,
)


def get_resolve():
    """Resolve injects `resolve` into scripts it runs; fall back to the module."""
    injected = globals().get("resolve")
    if injected is not None:
        return injected
    try:
        import DaVinciResolveScript as bmd
    except ImportError:
        raise PlanError(
            "this script must be run from inside DaVinci Resolve "
            "(Workspace > Scripts), where its scripting module is on the path"
        )
    found = bmd.scriptapp("Resolve")
    if found is None:
        raise PlanError("Resolve is not running, or scripting is disabled in its preferences")
    return found


def timeline_fps(project):
    """The project's frame rate, which the timeline will be built at.

    Asking the plan instead would build a timeline whose frames do not line up
    with the project's, and every cut would land a fraction late.
    """
    value = project.GetSetting("timelineFrameRate")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise PlanError(
            "could not read the project's frame rate (got {!r}). Set it in "
            "Project Settings first.".format(value)
        )


def import_source(resolve, project, path):
    """Put the broadcast in the media pool, or find it if it is already there."""
    media_pool = project.GetMediaPool()
    root = media_pool.GetRootFolder()
    wanted = os.path.basename(path)
    for item in root.GetClipList() or []:
        if item.GetName() == wanted:
            return item

    if not os.path.exists(path):
        raise PlanError(
            "the plan's source is not at {}. Move it back, or re-run the plan "
            "against its new location.".format(path)
        )
    added = resolve.GetMediaStorage().AddItemListToMediaPool([path])
    if not added:
        raise PlanError("Resolve would not import {}".format(path))
    return added[0]


def build(plan_path):
    """Read a plan and leave a timeline in the current project."""
    resolve = get_resolve()
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        raise PlanError("open a project in Resolve first")

    plan = load_plan(plan_path)
    fps = timeline_fps(project)
    item = import_source(resolve, project, source_path(plan))

    entries = clip_list(plan, fps, media_pool_item=item)
    timeline = project.GetMediaPool().CreateTimelineFromClips(timeline_name(plan), entries)
    if not timeline:
        raise PlanError(
            "Resolve refused to build the timeline. The usual cause is a clip whose "
            "frame rate differs from the project's."
        )

    print(summary(plan, fps))
    for start, end in dropped_spans(plan, fps):
        print("  skipped {:.3f}-{:.3f}s: shorter than one frame at {} fps".format(start, end, fps))

    srt = subtitle_path(plan_path)
    if srt:
        # Subtitles are a sidecar: `aicut export --format srt` writes them
        # beside the plan, and Resolve imports them onto their own track.
        if not timeline.ImportIntoTimeline(srt, {"importType": "subtitle"}):
            print("  the subtitles at {} were not imported; add them by hand".format(srt))
    else:
        print("  no .srt beside the plan; run `aicut export <plan> --format srt` for captions")
    return timeline


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        plan_path = argv[0]
    else:
        plan_path = _ask_for_plan()
        if not plan_path:
            return 1
    try:
        build(plan_path)
    except PlanError as exc:
        print("aicut: {}".format(exc))
        return 1
    return 0


def _ask_for_plan():
    """Resolve runs scripts with no arguments from its menu, so ask there."""
    try:
        fusion = get_resolve().Fusion()
        dialog = fusion.RequestFile("", "", {"FReqB_SeqGather": False,
                                             "FReqS_Title": "aicut edit plan (.json)"})
        return dialog if isinstance(dialog, str) else None
    except Exception as exc:                       # pragma: no cover - UI path
        print("aicut: could not open a file dialog ({}); "
              "run the script with a path instead".format(exc))
        return None


if __name__ == "__main__":
    raise SystemExit(main())
