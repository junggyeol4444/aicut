/* Turn an aicut edit plan into the clip list Premiere Pro wants.
 *
 * Every decision the Premiere script makes lives here and nothing in this file
 * touches Premiere, so it runs under plain node - which is how it is tested on
 * a machine with no Adobe install on it, the machine this was written on.
 * `aicut_premiere.jsx` is the part that talks to the application.
 *
 * ES3 on purpose: ExtendScript is not modern JavaScript. No let, no const, no
 * arrow functions, no Array.prototype.map with holes, no JSON guarantee.
 */

/*global $ */

var aicutPlan = (function () {
    "use strict";

    // Premiere counts time in ticks, not seconds or frames. This number is
    // fixed in the application and is what every Time object converts through.
    var TICKS_PER_SECOND = 254016000000;

    // Premiere's out point is the first frame NOT included: a clip from frame
    // 0 to frame 30 at 30 fps is one second long. Resolve's endFrame is the
    // opposite (inclusive), which is exactly why both plugins state it out
    // loud instead of leaving it in the arithmetic.
    var OUT_POINT_IS_EXCLUSIVE = true;

    function PlanError(message) {
        this.name = "PlanError";
        this.message = message;
    }
    PlanError.prototype = new Error();

    function fail(message) {
        throw new PlanError(message);
    }

    function parse(text, where) {
        var plan;
        try {
            // ExtendScript has no JSON in older hosts, so eval is the fallback
            // every Adobe script uses. The input is a file aicut itself wrote.
            plan = (typeof JSON !== "undefined" && JSON.parse)
                ? JSON.parse(text)
                : eval("(" + text + ")");
        } catch (e) {
            fail((where || "the plan") + " is not valid JSON: " + e.message);
        }
        if (!plan || !plan.cuts) {
            fail((where || "this file") + " has no 'cuts'; it is not an aicut edit plan. "
                 + "Plans are written to <workspace>/<project>/plans/.");
        }
        if (!plan.cuts.length) {
            fail("this plan has no cuts in it - there is no sequence to build");
        }
        return plan;
    }

    function sourcePath(plan) {
        var path = plan.source_path || "";
        if (!path) {
            fail("the plan does not say which file it was cut from");
        }
        return path;
    }

    /* The parts of one cut that survive pacing, in source seconds. Mirrors
     * Cut.kept_spans in aicut: a cut carries spans the renderer must drop from
     * inside it (9.3), and a sequence that ignores them plays the dead air the
     * plan decided to remove. */
    function keptSpans(cut) {
        var spans = [[Number(cut.source_start_sec), Number(cut.source_end_sec)]];
        var removals = [];
        var raw = cut.remove_spans || [];
        var i, j, out, start, end, a, b;

        for (i = 0; i < raw.length; i++) {
            removals.push([Number(raw[i][0]), Number(raw[i][1])]);
        }
        removals.sort(function (x, y) { return x[0] - y[0]; });

        for (i = 0; i < removals.length; i++) {
            start = removals[i][0];
            end = removals[i][1];
            out = [];
            for (j = 0; j < spans.length; j++) {
                a = spans[j][0];
                b = spans[j][1];
                if (end <= a || start >= b) {
                    out.push([a, b]);
                    continue;
                }
                if (start > a) { out.push([a, Math.min(start, b)]); }
                if (end < b) { out.push([Math.max(end, a), b]); }
            }
            spans = out;
        }

        out = [];
        for (i = 0; i < spans.length; i++) {
            if (spans[i][1] - spans[i][0] > 1e-3) { out.push(spans[i]); }
        }
        return out;
    }

    function inPlanOrder(cuts) {
        var ordered = cuts.slice(0);
        ordered.sort(function (x, y) {
            return (x.sequence_order || 0) - (y.sequence_order || 0);
        });
        return ordered;
    }

    function ticks(seconds) {
        // A string, because ExtendScript's Number loses precision well before
        // 2.5e11 ticks a second does, and Premiere's Time.ticks is a string too.
        return String(Math.round(seconds * TICKS_PER_SECOND));
    }

    /* What the script inserts, in plan order.
     *
     * Order is the plan's sequence_order, not source time: 2.4 lets a video
     * open on the moment that happened last, and sorting by source would
     * quietly undo the structure the plan chose. */
    function clipList(plan, fps) {
        var entries = [];
        var cuts, cut, spans, i, j, startFrame, endFrame, inSec, outSec, timeline = 0;

        if (!(fps > 0)) {
            fail("frame rate must be positive, got " + fps);
        }
        cuts = inPlanOrder(plan.cuts);
        for (i = 0; i < cuts.length; i++) {
            cut = cuts[i];
            spans = keptSpans(cut);
            for (j = 0; j < spans.length; j++) {
                // Snapped to the sequence's own frame grid. Inserting at raw
                // seconds leaves sub-frame gaps that accumulate into a drift
                // no one can find by the end of a long timeline.
                startFrame = Math.round(spans[j][0] * fps);
                endFrame = Math.round(spans[j][1] * fps);
                if (endFrame <= startFrame) {
                    continue;               // shorter than one frame; reported below
                }
                inSec = startFrame / fps;
                outSec = endFrame / fps;
                entries.push({
                    inSeconds: inSec,
                    outSeconds: outSec,
                    timelineSeconds: timeline,
                    inTicks: ticks(inSec),
                    outTicks: ticks(outSec),
                    timelineTicks: ticks(timeline),
                    frames: endFrame - startFrame
                });
                timeline += (endFrame - startFrame) / fps;
            }
        }
        if (!entries.length) {
            fail("every cut in this plan is shorter than one frame at " + fps + " fps");
        }
        return entries;
    }

    function droppedSpans(plan, fps) {
        var dropped = [];
        var cuts = inPlanOrder(plan.cuts);
        var i, j, spans;
        for (i = 0; i < cuts.length; i++) {
            spans = keptSpans(cuts[i]);
            for (j = 0; j < spans.length; j++) {
                if (Math.round(spans[j][1] * fps) <= Math.round(spans[j][0] * fps)) {
                    dropped.push(spans[j]);
                }
            }
        }
        return dropped;
    }

    function baseName(path) {
        var text = String(path).replace(/\\/g, "/");
        var parts = text.split("/");
        return parts[parts.length - 1] || text;
    }

    function sequenceName(plan) {
        var episode = String(plan.episode_id || "episode");
        var target = plan.target_type || "cut";
        return "aicut " + target + " " + episode.substring(0, 8);
    }

    /* Where `aicut export --format srt` puts the subtitles for this plan. */
    function subtitlePath(planPath) {
        var text = String(planPath);
        var dot = text.lastIndexOf(".");
        var slash = Math.max(text.lastIndexOf("/"), text.lastIndexOf("\\"));
        return (dot > slash ? text.substring(0, dot) : text) + ".srt";
    }

    function summary(plan, fps) {
        var entries = clipList(plan, fps);
        var frames = 0;
        var i;
        for (i = 0; i < entries.length; i++) { frames += entries[i].frames; }
        return entries.length + " clips, " + (frames / fps).toFixed(1) + "s at "
            + fps + " fps, from " + baseName(sourcePath(plan));
    }

    return {
        TICKS_PER_SECOND: TICKS_PER_SECOND,
        OUT_POINT_IS_EXCLUSIVE: OUT_POINT_IS_EXCLUSIVE,
        PlanError: PlanError,
        parse: parse,
        sourcePath: sourcePath,
        keptSpans: keptSpans,
        clipList: clipList,
        droppedSpans: droppedSpans,
        baseName: baseName,
        sequenceName: sequenceName,
        subtitlePath: subtitlePath,
        summary: summary,
        ticks: ticks
    };
}());

// node, for the tests. ExtendScript has no module and must not see this.
if (typeof module !== "undefined" && module.exports) { module.exports = aicutPlan; }
