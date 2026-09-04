"""SQLite persistence for the data model of 13장.

Keeps the JSON-in-column columns honest by doing the encode/decode in one
place, and gives every pipeline stage the same read/write surface so a stage
can be re-run alone (16장: a failed render must not cost the edit plan).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from aicut.models import (
    ContentCandidate,
    Cut,
    DetailSpan,
    Decision,
    Episode,
    Event,
    EventMention,
    PacingMode,
    Project,
    SituationLabel,
    SituationSpan,
    SubtitleLine,
    Utterance,
    WindowSummary,
    new_id,
)

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _u(text: str | None, fallback: Any) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return fallback


class Store:
    """Thin repository over SQLite. One instance per workspace database."""

    def __init__(self, path: str | Path = ":memory:", *, timeout: float = 30.0):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=timeout)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            # The UI runs a pipeline on a worker thread while the browser polls
            # from request threads; each thread opens its own connection (SQLite
            # forbids sharing one), so WAL keeps a reader from blocking on the
            # writer and busy_timeout absorbs the overlap.
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        self.migrate()

    def migrate(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- project -----------------------------------------------------------
    def create_project(self, project: Project) -> Project:
        project.created_at = project.created_at or _now()
        self.conn.execute(
            "INSERT INTO tb_project (project_id, file_path, duration_sec, status, profile_name,"
            " channel_ref, length_hint_sec, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                project.project_id,
                project.file_path,
                project.duration_sec,
                project.status,
                project.profile_name,
                project.channel_ref,
                project.length_hint_sec,
                project.created_at,
            ),
        )
        self.conn.commit()
        return project

    def get_project(self, project_id: str) -> Project | None:
        row = self.conn.execute("SELECT * FROM tb_project WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            return None
        return Project(
            project_id=row["project_id"],
            file_path=row["file_path"],
            duration_sec=row["duration_sec"],
            status=row["status"],
            profile_name=row["profile_name"],
            channel_ref=row["channel_ref"],
            length_hint_sec=row["length_hint_sec"],
            created_at=row["created_at"],
        )

    def list_projects(self) -> list[Project]:
        ids = [r["project_id"] for r in self.conn.execute("SELECT project_id FROM tb_project ORDER BY created_at")]
        return [p for p in (self.get_project(i) for i in ids) if p is not None]

    def set_status(self, project_id: str, state: str, detail: str = "") -> None:
        self.conn.execute("UPDATE tb_project SET status=? WHERE project_id=?", (state, project_id))
        self.conn.execute(
            "INSERT INTO tb_state_log (project_id, state, detail, at) VALUES (?,?,?,?)",
            (project_id, state, detail, _now()),
        )
        self.conn.commit()

    def state_log(self, project_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT state, detail, at FROM tb_state_log WHERE project_id=? ORDER BY log_id", (project_id,)
        )]

    def set_duration(self, project_id: str, duration_sec: float) -> None:
        self.conn.execute("UPDATE tb_project SET duration_sec=? WHERE project_id=?", (duration_sec, project_id))
        self.conn.commit()

    # ---- utterances --------------------------------------------------------
    def replace_utterances(self, project_id: str, utterances: Sequence[Utterance]) -> None:
        self.conn.execute("DELETE FROM tb_utterance WHERE project_id=?", (project_id,))
        self.conn.executemany(
            "INSERT INTO tb_utterance (project_id, start_sec, end_sec, speaker, track, text, words_json)"
            " VALUES (?,?,?,?,?,?,?)",
            [(project_id, u.start_sec, u.end_sec, u.speaker, u.track, u.text, _j(u.words)) for u in utterances],
        )
        self.conn.commit()

    def utterances(self, project_id: str, start_sec: float | None = None, end_sec: float | None = None) -> list[Utterance]:
        sql = "SELECT * FROM tb_utterance WHERE project_id=?"
        args: list[Any] = [project_id]
        if start_sec is not None:
            sql += " AND end_sec >= ?"
            args.append(start_sec)
        if end_sec is not None:
            sql += " AND start_sec <= ?"
            args.append(end_sec)
        sql += " ORDER BY start_sec"
        return [
            Utterance(
                start_sec=r["start_sec"],
                end_sec=r["end_sec"],
                text=r["text"],
                speaker=r["speaker"],
                track=r["track"],
                words=_u(r["words_json"], []),
            )
            for r in self.conn.execute(sql, args)
        ]

    # ---- situations / windows / details ------------------------------------
    def replace_situations(self, project_id: str, spans: Sequence[SituationSpan]) -> None:
        self.conn.execute("DELETE FROM tb_situation_span WHERE project_id=?", (project_id,))
        self.conn.executemany(
            "INSERT INTO tb_situation_span (project_id, start_sec, end_sec, label, speakers, evidence)"
            " VALUES (?,?,?,?,?,?)",
            [(project_id, s.start_sec, s.end_sec, s.label.value, _j(s.speakers), _j(s.evidence)) for s in spans],
        )
        self.conn.commit()

    def situations(self, project_id: str) -> list[SituationSpan]:
        return [
            SituationSpan(
                start_sec=r["start_sec"],
                end_sec=r["end_sec"],
                label=SituationLabel(r["label"]),
                speakers=_u(r["speakers"], []),
                evidence=_u(r["evidence"], {}),
            )
            for r in self.conn.execute(
                "SELECT * FROM tb_situation_span WHERE project_id=? ORDER BY start_sec", (project_id,)
            )
        ]

    def replace_windows(self, project_id: str, windows: Sequence[WindowSummary]) -> None:
        self.conn.execute("DELETE FROM tb_window_summary WHERE project_id=?", (project_id,))
        self.conn.executemany(
            "INSERT INTO tb_window_summary (project_id, start_sec, end_sec, summary, people, topics, screen,"
            " notable, notable_reason, tension_peak, markers) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    project_id, w.start_sec, w.end_sec, w.summary, _j(w.people), _j(w.topics), w.screen,
                    int(w.notable), w.notable_reason, w.tension_peak, _j(w.markers),
                )
                for w in windows
            ],
        )
        self.conn.commit()

    def windows(self, project_id: str) -> list[WindowSummary]:
        return [
            WindowSummary(
                start_sec=r["start_sec"], end_sec=r["end_sec"], summary=r["summary"],
                people=_u(r["people"], []), topics=_u(r["topics"], []), screen=r["screen"],
                notable=bool(r["notable"]), notable_reason=r["notable_reason"],
                tension_peak=r["tension_peak"], markers=_u(r["markers"], []),
            )
            for r in self.conn.execute(
                "SELECT * FROM tb_window_summary WHERE project_id=? ORDER BY start_sec", (project_id,)
            )
        ]

    def replace_details(self, project_id: str, details: Sequence[DetailSpan]) -> None:
        self.conn.execute("DELETE FROM tb_detail_span WHERE project_id=?", (project_id,))
        self.conn.executemany(
            "INSERT INTO tb_detail_span (project_id, start_sec, end_sec, exact_start_sec, exact_end_sec, beats, notes)"
            " VALUES (?,?,?,?,?,?,?)",
            [(project_id, d.start_sec, d.end_sec, d.exact_start_sec, d.exact_end_sec, _j(d.beats), d.notes) for d in details],
        )
        self.conn.commit()

    def details(self, project_id: str) -> list[DetailSpan]:
        return [
            DetailSpan(
                start_sec=r["start_sec"], end_sec=r["end_sec"],
                exact_start_sec=r["exact_start_sec"], exact_end_sec=r["exact_end_sec"],
                beats=_u(r["beats"], []), notes=r["notes"],
            )
            for r in self.conn.execute(
                "SELECT * FROM tb_detail_span WHERE project_id=? ORDER BY start_sec", (project_id,)
            )
        ]

    # ---- events (long-term memory) ----------------------------------------
    def replace_events(self, project_id: str, events: Sequence[Event]) -> None:
        self.conn.execute("DELETE FROM tb_event WHERE project_id=?", (project_id,))
        for event in events:
            self.conn.execute(
                "INSERT INTO tb_event (event_id, project_id, summary, people, relations) VALUES (?,?,?,?,?)",
                (event.event_id, project_id, event.summary, _j(event.people), _j(event.relations)),
            )
            self.conn.executemany(
                "INSERT INTO tb_event_mention (event_id, source_start_sec, source_end_sec, role, quote)"
                " VALUES (?,?,?,?,?)",
                [(event.event_id, m.source_start_sec, m.source_end_sec, m.role, m.quote) for m in event.mentions],
            )
        self.conn.commit()

    def events(self, project_id: str) -> list[Event]:
        out: list[Event] = []
        for r in self.conn.execute("SELECT * FROM tb_event WHERE project_id=?", (project_id,)):
            mentions = [
                EventMention(
                    event_id=m["event_id"], source_start_sec=m["source_start_sec"],
                    source_end_sec=m["source_end_sec"], role=m["role"], quote=m["quote"],
                    mention_id=m["mention_id"],
                )
                for m in self.conn.execute(
                    "SELECT * FROM tb_event_mention WHERE event_id=? ORDER BY source_start_sec", (r["event_id"],)
                )
            ]
            out.append(Event(
                event_id=r["event_id"], project_id=project_id, summary=r["summary"],
                people=_u(r["people"], []), relations=_u(r["relations"], []), mentions=mentions,
            ))
        out.sort(key=lambda e: e.span()[0])
        return out

    # ---- candidates --------------------------------------------------------
    def replace_candidates(self, project_id: str, candidates: Sequence[ContentCandidate]) -> None:
        self.conn.execute("DELETE FROM tb_content_candidate WHERE project_id=?", (project_id,))
        self.upsert_candidates(project_id, candidates)

    def upsert_candidates(self, project_id: str, candidates: Sequence[ContentCandidate]) -> None:
        self.conn.executemany(
            "INSERT INTO tb_content_candidate (candidate_id, project_id, core_summary,"
            " related_event_ids, required_context, required_context_sec, independence_score, density_score,"
            " has_resolution, decision, decision_reason, combine_with, human_verdict)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(candidate_id) DO UPDATE SET"
            " project_id=excluded.project_id, core_summary=excluded.core_summary,"
            " related_event_ids=excluded.related_event_ids, required_context=excluded.required_context,"
            " required_context_sec=excluded.required_context_sec,"
            " independence_score=excluded.independence_score, density_score=excluded.density_score,"
            " has_resolution=excluded.has_resolution, decision=excluded.decision,"
            " decision_reason=excluded.decision_reason, combine_with=excluded.combine_with,"
            " human_verdict=excluded.human_verdict",
            [
                (
                    c.candidate_id, project_id, c.core_summary, _j(c.related_event_ids), c.required_context,
                    c.required_context_sec, c.independence_score, c.density_score, int(c.has_resolution),
                    c.decision.value, c.decision_reason, _j(c.combine_with), c.human_verdict,
                )
                for c in candidates
            ],
        )
        self.conn.commit()

    def candidates(self, project_id: str) -> list[ContentCandidate]:
        return [
            ContentCandidate(
                candidate_id=r["candidate_id"], project_id=project_id, core_summary=r["core_summary"],
                related_event_ids=_u(r["related_event_ids"], []), required_context=r["required_context"],
                required_context_sec=r["required_context_sec"], independence_score=r["independence_score"],
                density_score=r["density_score"], has_resolution=bool(r["has_resolution"]),
                decision=Decision(r["decision"]), decision_reason=r["decision_reason"],
                combine_with=_u(r["combine_with"], []), human_verdict=r["human_verdict"],
            )
            for r in self.conn.execute(
                "SELECT * FROM tb_content_candidate WHERE project_id=? ORDER BY independence_score DESC", (project_id,)
            )
        ]

    def set_human_verdict(self, candidate_id: str, verdict: str) -> None:
        self.conn.execute(
            "UPDATE tb_content_candidate SET human_verdict=? WHERE candidate_id=?", (verdict, candidate_id)
        )
        self.conn.commit()

    # ---- episodes ----------------------------------------------------------
    def save_episode(self, episode: Episode) -> Episode:
        # Upsert, never INSERT OR REPLACE: REPLACE deletes the existing row
        # first, and ON DELETE CASCADE then takes every child with it - the
        # performance rows of loop C and any queued upload would disappear
        # every time an episode was saved again.
        self.conn.execute(
            "INSERT INTO tb_episode (episode_id, project_id, candidate_ids, title_candidates,"
            " planned_structure, target_type, planned_duration_sec, output_mp4_path, thumbnail_path,"
            " thumbnail_candidates, metadata, render_status, review_status, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(episode_id) DO UPDATE SET"
            " project_id=excluded.project_id, candidate_ids=excluded.candidate_ids,"
            " title_candidates=excluded.title_candidates, planned_structure=excluded.planned_structure,"
            " target_type=excluded.target_type, planned_duration_sec=excluded.planned_duration_sec,"
            " output_mp4_path=excluded.output_mp4_path, thumbnail_path=excluded.thumbnail_path,"
            " thumbnail_candidates=excluded.thumbnail_candidates, metadata=excluded.metadata,"
            " render_status=excluded.render_status, review_status=excluded.review_status,"
            " notes=excluded.notes",
            (
                episode.episode_id, episode.project_id, _j(episode.candidate_ids), _j(episode.title_candidates),
                _j(episode.planned_structure), episode.target_type, episode.planned_duration_sec,
                episode.output_mp4_path, episode.thumbnail_path, _j(episode.thumbnail_candidates),
                _j(episode.metadata), episode.render_status, episode.review_status, episode.notes,
            ),
        )
        self.conn.execute("DELETE FROM tb_edit_timeline WHERE episode_id=?", (episode.episode_id,))
        self.conn.executemany(
            "INSERT INTO tb_edit_timeline (episode_id, sequence_order, source_start_sec, source_end_sec,"
            " speaker_tag, scene_role, pacing_mode, pacing_reason, visual_effect, audio_effect, subtitle_ref, silences,"
            " remove_spans) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    episode.episode_id, c.sequence_order, c.source_start_sec, c.source_end_sec, c.speaker_tag,
                    c.scene_role, c.pacing_mode.value, c.pacing_reason, _j(c.visual_effect), _j(c.audio_effect),
                    c.subtitle_ref, _j(c.silences), _j(c.remove_spans),
                )
                for c in episode.timeline
            ],
        )
        self.conn.execute("DELETE FROM tb_subtitle WHERE episode_id=?", (episode.episode_id,))
        self.conn.executemany(
            "INSERT INTO tb_subtitle (episode_id, start_sec, end_sec, text, speaker, emphasis, style)"
            " VALUES (?,?,?,?,?,?,?)",
            [
                (episode.episode_id, s.start_sec, s.end_sec, s.text, s.speaker, int(s.emphasis), s.style)
                for s in episode.subtitles
            ],
        )
        self.conn.commit()
        return episode

    def episodes(self, project_id: str) -> list[Episode]:
        ids = [r["episode_id"] for r in self.conn.execute(
            "SELECT episode_id FROM tb_episode WHERE project_id=?", (project_id,)
        )]
        return [e for e in (self.get_episode(i) for i in ids) if e is not None]

    def get_episode(self, episode_id: str) -> Episode | None:
        r = self.conn.execute("SELECT * FROM tb_episode WHERE episode_id=?", (episode_id,)).fetchone()
        if r is None:
            return None
        timeline = [
            Cut(
                sequence_order=c["sequence_order"], source_start_sec=c["source_start_sec"],
                source_end_sec=c["source_end_sec"], speaker_tag=c["speaker_tag"], scene_role=c["scene_role"],
                pacing_mode=PacingMode(c["pacing_mode"]), pacing_reason=c["pacing_reason"],
                visual_effect=_u(c["visual_effect"], {}), audio_effect=_u(c["audio_effect"], {}),
                subtitle_ref=c["subtitle_ref"], silences=_u(c["silences"], []),
                remove_spans=_u(c["remove_spans"], []), cut_id=c["cut_id"],
            )
            for c in self.conn.execute(
                "SELECT * FROM tb_edit_timeline WHERE episode_id=? ORDER BY sequence_order", (episode_id,)
            )
        ]
        subtitles = [
            SubtitleLine(
                start_sec=s["start_sec"], end_sec=s["end_sec"], text=s["text"], speaker=s["speaker"],
                emphasis=bool(s["emphasis"]), style=s["style"],
            )
            for s in self.conn.execute(
                "SELECT * FROM tb_subtitle WHERE episode_id=? ORDER BY start_sec", (episode_id,)
            )
        ]
        return Episode(
            episode_id=r["episode_id"], project_id=r["project_id"], candidate_ids=_u(r["candidate_ids"], []),
            title_candidates=_u(r["title_candidates"], []), planned_structure=_u(r["planned_structure"], {}),
            target_type=r["target_type"], planned_duration_sec=r["planned_duration_sec"],
            timeline=timeline, subtitles=subtitles, output_mp4_path=r["output_mp4_path"],
            thumbnail_path=r["thumbnail_path"], thumbnail_candidates=_u(r["thumbnail_candidates"], []),
            metadata=_u(r["metadata"], {}), render_status=r["render_status"], review_status=r["review_status"],
            notes=r["notes"],
        )

    # ---- learning loops ----------------------------------------------------
    def save_reference(self, video_id: str, channel_id: str, public_metrics: dict, patterns: dict) -> str:
        ref_id = new_id()
        self.conn.execute(
            "INSERT INTO tb_yt_reference (ref_id, video_id, channel_id, public_metrics, extracted_patterns, analyzed_at)"
            " VALUES (?,?,?,?,?,?)",
            (ref_id, video_id, channel_id, _j(public_metrics), _j(patterns), _now()),
        )
        self.conn.commit()
        return ref_id

    def references(self) -> list[dict[str, Any]]:
        return [
            {
                "ref_id": r["ref_id"], "video_id": r["video_id"], "channel_id": r["channel_id"],
                "public_metrics": _u(r["public_metrics"], {}), "extracted_patterns": _u(r["extracted_patterns"], {}),
                "analyzed_at": r["analyzed_at"],
            }
            for r in self.conn.execute("SELECT * FROM tb_yt_reference ORDER BY analyzed_at")
        ]

    def save_source_output_pair(self, source_ref: str, output_ref: str, analysis: dict) -> str:
        pair_id = new_id()
        self.conn.execute(
            "INSERT INTO tb_source_output_pair (pair_id, source_ref, output_ref, selection_analysis, created_at)"
            " VALUES (?,?,?,?,?)",
            (pair_id, source_ref, output_ref, _j(analysis), _now()),
        )
        self.conn.commit()
        return pair_id

    def source_output_pairs(self) -> list[dict[str, Any]]:
        return [
            {
                "pair_id": r["pair_id"], "source_ref": r["source_ref"], "output_ref": r["output_ref"],
                "selection_analysis": _u(r["selection_analysis"], {}), "created_at": r["created_at"],
            }
            for r in self.conn.execute("SELECT * FROM tb_source_output_pair ORDER BY created_at")
        ]

    def save_performance(self, episode_id: str, metrics: dict) -> str:
        perf_id = new_id()
        self.conn.execute(
            "INSERT INTO tb_performance (perf_id, episode_id, metrics, collected_at) VALUES (?,?,?,?)",
            (perf_id, episode_id, _j(metrics), _now()),
        )
        self.conn.commit()
        return perf_id

    def performance(self, episode_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tb_performance"
        args: tuple = ()
        if episode_id:
            sql += " WHERE episode_id=?"
            args = (episode_id,)
        return [
            {"perf_id": r["perf_id"], "episode_id": r["episode_id"], "metrics": _u(r["metrics"], {}),
             "collected_at": r["collected_at"]}
            for r in self.conn.execute(sql + " ORDER BY collected_at", args)
        ]

    # ---- calibration profiles ---------------------------------------------
    def save_profile(self, name: str, channel_ref: str, params: dict, measured_at: str | None, eval_score: dict) -> str:
        profile_id = new_id()
        self.conn.execute(
            "INSERT INTO tb_calibration_profile (profile_id, channel_ref, name, params, measured_at, eval_score)"
            " VALUES (?,?,?,?,?,?)",
            (profile_id, channel_ref, name, _j(params), measured_at, _j(eval_score)),
        )
        self.conn.commit()
        return profile_id

    def profiles(self) -> list[dict[str, Any]]:
        return [
            {"profile_id": r["profile_id"], "channel_ref": r["channel_ref"], "name": r["name"],
             "params": _u(r["params"], {}), "measured_at": r["measured_at"], "eval_score": _u(r["eval_score"], {})}
            for r in self.conn.execute("SELECT * FROM tb_calibration_profile ORDER BY measured_at")
        ]

    # ---- quota ledger / upload queue (11.4, 16장) --------------------------
    def record_quota(self, pt_date: str, units: int, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO tb_quota_usage (pt_date, units, reason, at) VALUES (?,?,?,?)",
            (pt_date, units, reason, _now()),
        )
        self.conn.commit()

    def quota_used(self, pt_date: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(units),0) AS used FROM tb_quota_usage WHERE pt_date=?", (pt_date,)
        ).fetchone()
        return int(row["used"])

    def enqueue_upload(self, episode_id: str, retry_after: str | None, error: str, state: str = "RETRY_QUEUED") -> None:
        """Queue an episode for retry, or refresh the entry it already has."""
        self.conn.execute(
            "INSERT INTO tb_upload_queue (episode_id, state, retry_after, last_error, updated_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(episode_id) DO UPDATE SET state=excluded.state,"
            " retry_after=excluded.retry_after, last_error=excluded.last_error,"
            " updated_at=excluded.updated_at",
            (episode_id, state, retry_after, error, _now()),
        )
        self.conn.commit()

    def upload_queue(self, state: str = "RETRY_QUEUED") -> list[dict[str, Any]]:
        return [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM tb_upload_queue WHERE state=? ORDER BY queue_id", (state,)
            )
        ]

    def set_queue_state(self, queue_id: int, state: str, error: str = "") -> None:
        self.conn.execute(
            "UPDATE tb_upload_queue SET state=?, last_error=?, updated_at=? WHERE queue_id=?",
            (state, error, _now(), queue_id),
        )
        self.conn.commit()
