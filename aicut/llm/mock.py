"""Offline stand-in for the reasoning backend.

This is **not** a small model and does not pretend to judge anything. It applies
transparent heuristics to whatever payload a stage hands it, so the deterministic
half of the system - retrieval, timeline assembly, ASS generation, ffmpeg command
building, packaging, the state machine - can be exercised and tested end to end
without a model call. Every judgement it returns is labelled ``mock`` in its
reason field so it can never be mistaken for a real one in a report.
"""

from __future__ import annotations

from typing import Any

from aicut.llm.base import Producer


def _text_of(utterances: list[dict[str, Any]], limit: int = 3) -> str:
    return " ".join(u.get("text", "") for u in utterances[:limit]).strip()


def _keywords(text: str) -> set[str]:
    return {w.strip(".,!?\"'") for w in text.split() if len(w.strip(".,!?\"'")) > 1}


class MockProducer(Producer):
    name = "mock"

    def complete_json(self, task: str, system: str, payload: dict[str, Any]) -> Any:
        handler = getattr(self, f"_task_{task}", None)
        if handler is None:
            return {}
        return handler(payload)

    # -- 5장 -----------------------------------------------------------------
    def _task_summarize_window(self, payload: dict[str, Any]) -> dict[str, Any]:
        utterances = payload.get("utterances", [])
        trigger = payload.get("pass2_trigger", {})
        tension = float(payload.get("tension_peak", 0.0))
        markers = list(payload.get("signal_markers", []))
        notable = tension >= float(trigger.get("min_tension_peak", 1.1)) or len(markers) >= int(
            trigger.get("min_marker_count", 99)
        )
        return {
            "summary": _text_of(utterances) or payload.get("situation", "no speech in this window"),
            "people": sorted({u.get("speaker", "UNKNOWN") for u in utterances}),
            "topics": sorted(_keywords(_text_of(utterances, 8)))[:5],
            "screen": payload.get("situation", "unknown"),
            "notable": bool(notable),
            "notable_reason": f"mock: tension_peak={tension:.2f}, markers={markers}",
            "markers": markers,
        }

    def _task_detail_window(self, payload: dict[str, Any]) -> dict[str, Any]:
        utterances = payload.get("utterances", [])
        beats = [
            {"at_sec": u.get("start_sec", 0.0), "what": u.get("text", ""), "who": u.get("speaker", "UNKNOWN")}
            for u in utterances
        ]
        return {
            "exact_start_sec": utterances[0]["start_sec"] if utterances else payload.get("start_sec"),
            "exact_end_sec": utterances[-1]["end_sec"] if utterances else payload.get("end_sec"),
            "beats": beats,
            "notes": "mock detail pass",
        }

    def _task_build_events(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Group windows that share vocabulary; that is enough to exercise 5.4 linking."""
        # Windows where nobody said anything and nothing was marked carry no
        # event; grouping them would manufacture one "event" per empty window.
        windows = [
            w for w in payload.get("windows", [])
            if w.get("topics") or w.get("notable") or w.get("markers")
        ]
        groups: list[dict[str, Any]] = []
        for w in windows:
            topics = set(w.get("topics", []))
            target = None
            for g in groups:
                if topics & g["topics"]:
                    target = g
                    break
            if target is None:
                target = {"topics": set(topics), "windows": [], "people": set()}
                groups.append(target)
            target["topics"] |= topics
            target["people"] |= set(w.get("people", []))
            target["windows"].append(w)
        events = []
        for g in groups:
            wins = g["windows"]
            mentions = []
            for i, w in enumerate(wins):
                if i == 0:
                    role = "first_mention"
                elif i == len(wins) - 1 and len(wins) > 1:
                    role = "result"
                else:
                    role = "related_talk"
                mentions.append({
                    "source_start_sec": w["start_sec"],
                    "source_end_sec": w["end_sec"],
                    "role": role,
                    "quote": w.get("summary", "")[:120],
                })
            events.append({
                "summary": wins[0].get("summary", "")[:160],
                "people": sorted(g["people"]),
                "mentions": mentions,
                "relations": [],
            })
        return events

    def _task_merge_events(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        events = payload.get("events", [])
        groups: list[dict[str, Any]] = []
        for index, event in enumerate(events):
            people = set(event.get("people", []))
            topics = _keywords(event.get("summary", ""))
            target = None
            for group in groups:
                if (people & group["people"]) and (topics & group["topics"]):
                    target = group
                    break
            if target is None:
                target = {"people": set(people), "topics": set(topics), "members": []}
                groups.append(target)
            target["people"] |= people
            target["topics"] |= topics
            target["members"].append(index)
        return [
            {
                "member_indices": g["members"],
                "summary": events[g["members"][0]].get("summary", ""),
                "people": sorted(g["people"]),
                "relations": [],
            }
            for g in groups
        ]

    # -- 6장 -----------------------------------------------------------------
    def _task_discover_candidates(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for event in payload.get("events", []):
            mentions = event.get("mentions", [])
            # Stand-in for "is there enough here": how many separate moments the
            # event has, not how much of the broadcast it covers.
            density = min(1.0, len(mentions) / 3.0)
            out.append({
                "core_summary": event.get("summary", ""),
                "related_event_ids": [event.get("event_id", "")],
                "required_context": "",
                "required_context_sec": 0.0,
                "independence_score": min(1.0, 0.3 + 0.2 * len(mentions)),
                "density_score": density,
                "has_resolution": any(m.get("role") == "result" for m in mentions),
                "reason": "mock: scored from how many separate moments the event has",
            })
        return out

    def _task_evaluate_candidates(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rules = payload.get("thresholds", {})
        min_ind = float(rules.get("min_independence_score", 0.55))
        min_density = float(rules.get("min_density_score", 0.4))
        max_ctx = float(rules.get("max_required_context_sec", 90))
        out = []
        for c in payload.get("candidates", []):
            if c.get("required_context_sec", 0) > max_ctx:
                decision, reason = "reject", "mock: needs more context than a standalone video can carry"
            elif not c.get("has_resolution", True):
                decision, reason = "combine", "mock: no resolution of its own; look for a related event"
            elif c.get("independence_score", 0) < min_ind or c.get("density_score", 0) < min_density:
                decision, reason = "reject", "mock: below the profile's independence/density floor"
            else:
                decision, reason = "produce", "mock: self-contained and dense enough"
            out.append({
                "candidate_id": c.get("candidate_id", ""),
                "decision": decision,
                "reason": reason,
                "combine_with": [],
            })
        return out

    # -- 7-9장 ---------------------------------------------------------------
    def _task_plan_structure(self, payload: dict[str, Any]) -> dict[str, Any]:
        mentions = payload.get("mentions", [])
        order = {"result": 0, "first_mention": 1, "related_talk": 2, "conflict": 3}
        ordered = sorted(mentions, key=lambda m: (order.get(m.get("role", ""), 9), m["source_start_sec"]))
        beats = [
            {
                "role": m.get("role", "scene"),
                "intent": f"mock beat from {m.get('role', 'scene')}",
                "query": (m.get("quote") or "")[:80],
                "must_include_event_id": m.get("event_id"),
                "hint_start_sec": m.get("source_start_sec"),
                "hint_end_sec": m.get("source_end_sec"),
            }
            for m in ordered
        ]
        planned = sum(b["hint_end_sec"] - b["hint_start_sec"] for b in beats) if beats else 0.0
        return {
            "structure_name": "mock:result_first" if beats and beats[0]["role"] == "result" else "mock:chronological",
            "rationale": "mock: result first when the event has one, otherwise source order",
            "target_type": "shorts" if planned < 60 else "long",
            "planned_duration_sec": planned,
            "length_note": "mock: length taken from the mentions, not from the user's hint",
            "beats": beats,
        }

    def _task_select_scene(self, payload: dict[str, Any]) -> dict[str, Any]:
        cands = payload.get("candidates", [])
        if not cands:
            return {"chosen_index": None, "reason": "mock: retrieval returned nothing"}
        best = cands[0]
        return {
            "chosen_index": 0,
            "start_sec": best["start_sec"],
            "end_sec": best["end_sec"],
            "speaker": best.get("speaker", "UNKNOWN"),
            "subtitle_emphasis": False,
            "reason": "mock: highest retrieval score",
        }

    def _task_judge_pacing(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"pacing_mode": payload.get("rule_suggestion", "TRIM"), "reason": "mock: deferred to the rule layer"}

    # -- 11장 ----------------------------------------------------------------
    def _task_package_metadata(self, payload: dict[str, Any]) -> dict[str, Any]:
        summary = (payload.get("core_summary") or "untitled").strip()
        short = summary[:60]
        chapters = [
            {"at_sec": c.get("output_start_sec", 0.0), "label": c.get("scene_role", "scene")}
            for c in payload.get("cuts", [])[:8]
        ]
        return {
            "titles": [short, f"{short} (mock alt)", f"mock: {short}"],
            "description": summary,
            "tags": sorted(_keywords(summary))[:10],
            "chapters": chapters,
        }

    # -- 12.3 ----------------------------------------------------------------
    def _task_analyze_reference(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "structure": {}, "editing": {}, "storytelling": {}, "scene_selection": {},
            "title_pattern": "", "thumbnail_pattern": "",
            "production_logic": "mock: no analysis performed",
        }

    def _task_compare_source_output(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"selected": [], "dropped": [], "reordered": [], "repeated": [], "emphasised": [],
                "inferred_rules": ["mock: no rules inferred"]}

    def _task_learn_from_performance(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"observations": ["mock: no observation"], "strategy_updates": []}
