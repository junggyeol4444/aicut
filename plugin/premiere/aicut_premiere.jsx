/* Premiere Pro: build a sequence from an aicut edit plan.
 *
 * Install by copying this folder's two files into Premiere's script folder:
 *
 *   Windows  %APPDATA%\Adobe\Premiere Pro\<version>\Scripts
 *   macOS    ~/Documents/Adobe/Premiere Pro/<version>/Scripts
 *
 * Then: File > Scripts > aicut_premiere.
 *
 * This is a plain ExtendScript file, not a CEP panel: no ZXP, no signing
 * certificate, no extension manager. Copy two files and it is installed.
 *
 * Every decision - which spans survive, what order they go in, how seconds
 * become the sequence's frames and Premiere's ticks - lives in
 * `aicut_plan.js`, which has tests that run without Premiere. This file is
 * only the part that talks to the application.
 *
 * NOT VERIFIED HERE. Premiere Pro is not present in the environment this was
 * written in, so these API calls are written against Adobe's ExtendScript
 * documentation and have not been executed. The arithmetic they depend on has
 * been. Treat the first run as the test.
 *
 * Two things to check on that first run, both stated as constants rather than
 * buried in the arithmetic so they are easy to flip:
 *   - `OUT_POINT_IS_EXCLUSIVE` in aicut_plan.js. If every clip lands one frame
 *     short, this is why.
 *   - `insertClip` on an empty track appends; if the sequence is not empty the
 *     script refuses rather than dropping clips into someone else's edit.
 */

/*global $, app, File, Folder, ProjectItem, Time, alert */

#include "aicut_plan.js"

(function () {
    "use strict";

    function say(message) {
        // A script with no panel has two ways to speak. ESTK gets the console,
        // the person running it from the menu gets the dialog.
        $.writeln(message);
    }

    function planFileFromDialog() {
        var chosen = File.openDialog("aicut edit plan (.json)", "*.json", false);
        return chosen ? chosen.fsName : null;
    }

    function readFile(path) {
        var file = new File(path);
        var text;
        if (!file.exists) {
            throw new aicutPlan.PlanError("no edit plan at " + path);
        }
        file.encoding = "UTF-8";
        file.open("r");
        text = file.read();
        file.close();
        return text;
    }

    function sequenceFps(sequence) {
        // Premiere gives the sequence's frame duration in ticks; the frame rate
        // is what the plan's seconds have to be snapped to. Asking the plan
        // instead would put every cut a fraction of a frame late.
        var frameTicks = Number(sequence.timebase);
        if (!(frameTicks > 0)) {
            throw new aicutPlan.PlanError(
                "could not read the sequence's frame rate. Open a sequence first."
            );
        }
        return aicutPlan.TICKS_PER_SECOND / frameTicks;
    }

    function findOrImport(project, path) {
        var wanted = aicutPlan.baseName(path);
        var root = project.rootItem;
        var i, item;
        for (i = 0; i < root.children.numItems; i++) {
            item = root.children[i];
            if (item.name === wanted) {
                return item;
            }
        }
        if (!new File(path).exists) {
            throw new aicutPlan.PlanError(
                "the plan's source is not at " + path + ". Move it back, or re-run "
                + "the plan against its new location."
            );
        }
        project.importFiles([path], true, project.getInsertionBin(), false);
        for (i = 0; i < root.children.numItems; i++) {
            item = root.children[i];
            if (item.name === wanted) {
                return item;
            }
        }
        throw new aicutPlan.PlanError("Premiere would not import " + path);
    }

    function timeAt(seconds) {
        var time = new Time();
        time.ticks = aicutPlan.ticks(seconds);
        return time;
    }

    function build(planPath) {
        var project = app.project;
        var sequence, fps, plan, item, entries, track, i, entry, dropped, srt;

        if (!project) {
            throw new aicutPlan.PlanError("open a project in Premiere first");
        }
        sequence = project.activeSequence;
        if (!sequence) {
            throw new aicutPlan.PlanError(
                "open or create a sequence first - the script builds into the active one, "
                + "at its frame rate"
            );
        }
        track = sequence.videoTracks[0];
        if (track.clips.numItems > 0) {
            // Appending into an edit somebody is working on is not a thing to
            // do quietly. A new sequence costs them one menu item.
            throw new aicutPlan.PlanError(
                "the active sequence's first video track already has clips in it. "
                + "Make an empty sequence for this plan so nothing of yours is moved."
            );
        }

        plan = aicutPlan.parse(readFile(planPath), planPath);
        fps = sequenceFps(sequence);
        item = findOrImport(project, aicutPlan.sourcePath(plan));
        entries = aicutPlan.clipList(plan, fps);

        for (i = 0; i < entries.length; i++) {
            entry = entries[i];
            // in/out live on the project item, and insertClip takes what is set
            // there - so they are set immediately before each insert, not once.
            item.setInPoint(entry.inTicks, 4);      // 4 = video and audio
            item.setOutPoint(entry.outTicks, 4);
            track.insertClip(item, timeAt(entry.timelineSeconds));
        }

        say(aicutPlan.summary(plan, fps));
        dropped = aicutPlan.droppedSpans(plan, fps);
        for (i = 0; i < dropped.length; i++) {
            say("  skipped " + dropped[i][0].toFixed(3) + "-" + dropped[i][1].toFixed(3)
                + "s: shorter than one frame at " + fps + " fps");
        }

        srt = aicutPlan.subtitlePath(planPath);
        if (new File(srt).exists) {
            // Premiere imports an .srt as a caption item; it still has to be
            // dragged onto a caption track, which no scripting API exposes.
            project.importFiles([srt], true, project.getInsertionBin(), false);
            say("  imported " + aicutPlan.baseName(srt) + " into the project - drag it "
                + "onto a caption track");
        } else {
            say("  no .srt beside the plan; run `aicut export <plan> --format srt` for captions");
        }
        return sequence;
    }

    function main() {
        var planPath = (typeof $.getenv === "function" && $.getenv("AICUT_PLAN"))
            || planFileFromDialog();
        if (!planPath) {
            return;
        }
        try {
            build(planPath);
        } catch (e) {
            // A dialog, because a script run from the menu has no console open
            // and a silent failure looks like the plugin did nothing.
            alert("aicut: " + (e.message || e));
            say("aicut: " + (e.message || e));
        }
    }

    main();
}());
