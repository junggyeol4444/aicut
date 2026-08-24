"""Local operator UI (15장).

The four screens of 15.1, served from the stdlib so the app stays a single
runnable program with no JS build step:

1. 입력 (15.2)      POST /api/projects
2. 진행 모니터 (15.3) GET  /api/jobs/<id>
3. 후보 검토 (15.4)   GET/POST /api/projects/<id>/candidates
4. 결과 (15.5)       GET  /api/projects/<id>/episodes, /plan, /report
                    POST /api/episodes/<id>/review   ← 11.3 gate

Deviation from 20.1, stated plainly: the plan names PyQt6 or Electron for the
desktop wrapper. This is an HTTP server plus one static page, because that runs
and is testable headless; a PyQt6 QWebEngineView (or Electron shell) can wrap
this same server later without the UI logic changing.

The server binds to localhost only and holds a single-user workspace. It carries
no authentication and must not be exposed to a network.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
import uuid
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from aicut.config import CalibrationProfile
from aicut.db.store import Store
from aicut.errors import AicutError
from aicut.intelligence.knowledge import ProductionKnowledge
from aicut.llm import get_producer
from aicut.media.stt import TranscriptFileTranscriber
from aicut.models import to_dict
from aicut.pipeline import review as review_mod
from aicut.pipeline.context import RunContext
from aicut.pipeline.runner import Pipeline
from aicut.pipeline.states import State
from aicut.render.editplan import EditPlan, describe
from aicut.ui.jobs import Job, JobRunner

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


class UiServer:
    """Holds the workspace and dispatches API calls. Transport-agnostic."""

    def __init__(
        self,
        workspace: str | Path = "workspace",
        *,
        profile_path: str | None = None,
        producer_name: str = "mock",
    ):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.profile_path = profile_path
        self.producer_name = producer_name
        self.jobs = JobRunner()
        self.db_path = self.workspace / "aicut.db"
        self._local = threading.local()
        self._stores: list[Store] = []
        self._stores_lock = threading.Lock()

    # ---- helpers -----------------------------------------------------------
    @property
    def store(self) -> Store:
        """A Store belonging to the calling thread.

        SQLite connections cannot cross threads, and this server has several:
        the pipeline worker plus one per in-flight request. Each gets its own
        connection to the same WAL database.
        """
        store = getattr(self._local, "store", None)
        if store is None:
            store = Store(self.db_path)
            self._local.store = store
            with self._stores_lock:
                self._stores.append(store)
        return store

    def profile(self) -> CalibrationProfile:
        return CalibrationProfile.load(self.profile_path)

    def pipeline(self) -> Pipeline:
        knowledge = ProductionKnowledge.load(self.workspace / "knowledge.json").summary_for_planner()
        return Pipeline(
            self.store,
            self.profile(),
            get_producer(self.producer_name),
            workspace=self.workspace,
            knowledge=knowledge,
        )

    def context(self, project_id: str) -> RunContext:
        project = self.store.get_project(project_id)
        if project is None:
            raise KeyError(f"unknown project {project_id}")
        return RunContext(
            project=project,
            store=self.store,
            profile=self.profile(),
            producer=get_producer(self.producer_name),
            workspace=self.workspace,
        )

    # ---- 15.2 input --------------------------------------------------------
    def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        source = (body.get("source") or "").strip()
        if not source:
            raise ValueError("source file path is required")
        if not Path(source).exists():
            raise ValueError(f"file not found: {source}")

        pipeline = self.pipeline()
        project = pipeline.submit(
            source,
            length_hint_sec=_optional_float(body.get("length_hint_sec")),
            channel_ref=body.get("channel_ref", "") or "",
        )
        transcript = body.get("transcript") or None
        render = bool(body.get("render", True))
        frames = bool(body.get("sample_frames", False))
        stop_after = body.get("stop_after") or None

        def work(job: Job):
            job.append("info", f"submitted {source}")
            # The worker owns its own connection; the pipeline built above holds
            # the submitting thread's, so rebind it before running.
            pipeline.store = self.store
            transcriber = TranscriptFileTranscriber(transcript) if transcript else None
            if transcriber is None:
                job.append(
                    "warn",
                    "no transcript supplied; STT must have been run separately or the project"
                    " will fall back to whatever utterances are already stored",
                )
            return pipeline.run(
                project,
                transcriber=transcriber,
                stop_after=State(stop_after) if stop_after else None,
                sample_frames=frames,
                render=render,
            )

        job = self.jobs.start(str(uuid.uuid4()), project.project_id, source, work)
        return {"job_id": job.job_id, "project_id": project.project_id}

    # ---- 15.3 monitor ------------------------------------------------------
    def job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"unknown job {job_id}")
        return job.to_dict()

    def projects(self) -> list[dict[str, Any]]:
        rows = []
        for project in self.store.list_projects():
            episodes = self.store.episodes(project.project_id)
            rows.append({
                "project_id": project.project_id,
                "file_path": project.file_path,
                "status": project.status,
                "duration_sec": project.duration_sec,
                "created_at": project.created_at,
                "episodes": len(episodes),
            })
        return rows

    # ---- 15.4 candidate review --------------------------------------------
    def candidates(self, project_id: str) -> dict[str, Any]:
        ctx = self.context(project_id)
        return {
            "candidates": review_mod.candidate_review(ctx),
            "agreement": review_mod.agreement_rate(ctx),
            "events": [
                {"event_id": e.event_id, "summary": e.summary, "span": list(e.span()),
                 "mentions": len(e.mentions), "people": e.people}
                for e in self.store.events(project_id)
            ],
        }

    def verdict(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        ctx = self.context(project_id)
        review_mod.record_candidate_verdict(
            ctx, body["candidate_id"], body["verdict"], body.get("note", "") or ""
        )
        return {"agreement": review_mod.agreement_rate(ctx)}

    # ---- 15.5 results ------------------------------------------------------
    def episodes(self, project_id: str) -> list[dict[str, Any]]:
        rows = []
        for episode in self.store.episodes(project_id):
            plan_path = self.workspace / project_id / "plans" / f"{episode.episode_id}.json"
            rows.append({
                "episode_id": episode.episode_id,
                "target_type": episode.target_type,
                "structure": episode.planned_structure.get("structure_name", ""),
                "rationale": episode.planned_structure.get("rationale", ""),
                "duration_sec": round(episode.planned_duration_sec, 1),
                "cuts": len(episode.timeline),
                "titles": episode.title_candidates,
                "thumbnails": episode.thumbnail_candidates,
                "output": episode.output_mp4_path,
                "render_status": episode.render_status,
                "review_status": episode.review_status,
                "notes": episode.notes,
                "plan_path": str(plan_path) if plan_path.exists() else None,
                "metadata": episode.metadata,
            })
        return rows

    def plan(self, episode_id: str) -> dict[str, Any]:
        episode = self.store.get_episode(episode_id)
        if episode is None:
            raise KeyError(f"unknown episode {episode_id}")
        path = self.workspace / episode.project_id / "plans" / f"{episode_id}.json"
        if not path.exists():
            raise KeyError(f"no edit plan written for {episode_id}")
        plan = EditPlan.load(path)
        return {"path": str(path), "readable": describe(plan), "plan": plan.to_dict()}

    def review(self, episode_id: str, body: dict[str, Any]) -> dict[str, Any]:
        episode = self.store.get_episode(episode_id)
        if episode is None:
            raise KeyError(f"unknown episode {episode_id}")
        ctx = self.context(episode.project_id)
        action = body.get("action")
        reviewer = (body.get("reviewer") or "").strip()
        if not reviewer:
            raise ValueError("a reviewer name is required; the gate records who released the video (11.3)")
        if action == "approve":
            updated = review_mod.approve(ctx, episode_id, reviewer=reviewer, note=body.get("note", "") or "")
        elif action == "reject":
            updated = review_mod.reject(ctx, episode_id, reviewer=reviewer, reason=body.get("note", "") or "")
        else:
            raise ValueError("action must be 'approve' or 'reject'")
        return {"episode_id": episode_id, "review_status": updated.review_status}

    def report(self, project_id: str) -> dict[str, Any]:
        path = self.workspace / project_id / "report.json"
        if not path.exists():
            raise KeyError(f"no report for {project_id} yet")
        return json.loads(path.read_text(encoding="utf-8"))

    def profile_info(self) -> dict[str, Any]:
        profile = self.profile()
        return {
            "name": profile.name,
            "source": str(profile.source_path),
            "measured_at": profile.measured_at,
            "provisional": sorted(profile.provisional),
            "measured": sorted(profile.measured),
            "producer": self.producer_name,
            "warning": (
                "unmeasured parameters are in use; run the 17.4 sweep before trusting output"
                if profile.provisional else ""
            ),
        }

    def close(self) -> None:
        with self._stores_lock:
            for store in self._stores:
                try:
                    store.close()
                except Exception:                    # a worker thread may still hold one
                    pass
            self._stores.clear()


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------
Route = tuple[re.Pattern[str], str, Callable[..., Any]]


class _Handler(BaseHTTPRequestHandler):
    server_version = "aicut"

    def __init__(self, ui: UiServer, *args, **kwargs):
        self.ui = ui
        self.routes: list[Route] = [
            (re.compile(r"^/api/profile$"), "GET", lambda: ui.profile_info()),
            (re.compile(r"^/api/projects$"), "GET", lambda: ui.projects()),
            (re.compile(r"^/api/projects$"), "POST", lambda body: ui.submit(body)),
            (re.compile(r"^/api/jobs$"), "GET", lambda: ui.jobs.list()),
            (re.compile(r"^/api/jobs/([\w-]+)$"), "GET", lambda job_id: ui.job(job_id)),
            (re.compile(r"^/api/projects/([\w-]+)/candidates$"), "GET", lambda pid: ui.candidates(pid)),
            (re.compile(r"^/api/projects/([\w-]+)/candidates$"), "POST", lambda pid, body: ui.verdict(pid, body)),
            (re.compile(r"^/api/projects/([\w-]+)/episodes$"), "GET", lambda pid: ui.episodes(pid)),
            (re.compile(r"^/api/projects/([\w-]+)/report$"), "GET", lambda pid: ui.report(pid)),
            (re.compile(r"^/api/episodes/([\w-]+)/plan$"), "GET", lambda eid: ui.plan(eid)),
            (re.compile(r"^/api/episodes/([\w-]+)/review$"), "POST", lambda eid, body: ui.review(eid, body)),
        ]
        super().__init__(*args, **kwargs)

    # -- plumbing ------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:      # quieter than the default
        log.debug("ui %s", fmt % args)

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(to_dict(payload), ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method: str, body: dict[str, Any] | None = None) -> None:
        path = urlparse(self.path).path
        for pattern, verb, handler in self.routes:
            if verb != method:
                continue
            match = pattern.match(path)
            if not match:
                continue
            args = list(match.groups())
            if body is not None:
                args.append(body)
            try:
                self._send(200, handler(*args))
            except KeyError as exc:
                self._send(404, {"error": str(exc)})
            except (ValueError, PermissionError, AicutError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:                      # never take the server down
                log.exception("ui request failed: %s %s", method, path)
                self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        if method == "GET":
            self._serve_static(path)
        else:
            self._send(404, {"error": f"no route for {method} {path}"})

    def _serve_static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / name).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._send(404, {"error": f"not found: {path}"})
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- verbs ---------------------------------------------------------------
    def do_GET(self) -> None:       # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:      # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "request body is not valid JSON"})
            return
        if not isinstance(body, dict):
            self._send(400, {"error": "request body must be a JSON object"})
            return
        self._dispatch("POST", body)


def serve(
    workspace: str | Path = "workspace",
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    profile_path: str | None = None,
    producer_name: str = "mock",
) -> tuple[ThreadingHTTPServer, UiServer]:
    """Start the UI. Localhost only - there is no auth on these endpoints."""
    ui = UiServer(workspace, profile_path=profile_path, producer_name=producer_name)
    httpd = ThreadingHTTPServer((host, port), partial(_Handler, ui))
    return httpd, ui


def _optional_float(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
