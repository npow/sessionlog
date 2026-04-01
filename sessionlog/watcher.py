"""Real-time ingestion worker using watchdog for file system events."""

import threading
import time
import traceback
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from sessionlog.config import get_source_specs


class _JsonlEventHandler(FileSystemEventHandler):
    """Watchdog event handler that signals the worker on JSONL changes."""

    def __init__(self, on_change):
        super().__init__()
        self._on_change = on_change

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".jsonl"):
            self._on_change()

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".jsonl"):
            self._on_change()


class IngestionWorker(threading.Thread):
    """Daemon thread that watches configured source dirs for changed JSONL files.

    Uses watchdog's Observer for real-time file system event watching instead
    of polling. When changes are detected, runs the fast pipeline (everything
    except LLM judging, which is expensive and user-triggered).

    A 30-second debounce cooldown prevents running the pipeline too frequently
    when many files change at once.

    The ``status`` attribute is a dict visible to other threads:
      {"state": "idle"/"ingesting"/"judging", "step": "...", "ready": True/False,
       "current": N, "total": N}
    """

    def __init__(
        self,
        run_immediately: bool = False,
        source_specs: list[tuple[str, Path]] | None = None,
    ):
        super().__init__(daemon=True, name="ingestion-worker")
        self._run_immediately = run_immediately
        self._source_specs = source_specs or get_source_specs()
        self._stop_event = threading.Event()
        self._change_event = threading.Event()
        self._last_pipeline_time: float = 0.0
        self._cooldown: float = 30.0  # seconds between pipeline runs
        self.status: dict = {
            "state": "idle",
            "step": "",
            "ready": True,
            "current": 0,
            "total": 0,
        }
        self._refresh_request: dict | None = None
        self._refresh_lock = threading.Lock()
        self._observer: Observer | None = None

    def stop(self):
        self._stop_event.set()
        self._change_event.set()  # Wake the run loop so it can exit
        if self._observer is not None:
            self._observer.stop()

    def request_refresh(self, concurrency: int = 12):
        """Request a full refresh (ingest + judge) from the UI thread.

        Non-blocking — sets a flag that the worker picks up on its next loop.
        """
        with self._refresh_lock:
            self._refresh_request = {"concurrency": concurrency}
        self._change_event.set()

    @property
    def is_busy(self) -> bool:
        return self.status.get("state") not in ("idle",)

    def _on_fs_change(self):
        """Called by the watchdog event handler on any JSONL modification."""
        self._change_event.set()

    def _start_observer(self):
        """Start the watchdog observer for any existing source directories."""
        handler = _JsonlEventHandler(self._on_fs_change)
        self._observer = Observer()
        scheduled = False
        for _source_name, source_dir in self._source_specs:
            if source_dir is None:
                continue
            if not source_dir.exists() or not source_dir.is_dir():
                continue
            self._observer.schedule(handler, str(source_dir), recursive=True)
            scheduled = True
        if not scheduled:
            self._observer = None
            return
        self._observer.start()

    def run(self):
        self._start_observer()

        if self._run_immediately:
            try:
                self._run_pipeline()
                self._last_pipeline_time = time.monotonic()
            except Exception:
                traceback.print_exc()
                self._set_idle()

        while not self._stop_event.is_set():
            # Wait for a change event or timeout (re-check observer health)
            self._change_event.wait(timeout=60.0)
            self._change_event.clear()

            if self._stop_event.is_set():
                break

            # Check for user-triggered refresh request first
            req = None
            with self._refresh_lock:
                if self._refresh_request:
                    req = self._refresh_request
                    self._refresh_request = None

            try:
                if req:
                    self._run_full_refresh(req.get("concurrency", 12))
                    self._last_pipeline_time = time.monotonic()
                else:
                    # Debounce: don't run more often than the cooldown allows
                    now = time.monotonic()
                    if now - self._last_pipeline_time >= self._cooldown:
                        self._run_pipeline()
                        self._last_pipeline_time = time.monotonic()
            except Exception:
                traceback.print_exc()
                self._set_idle()

        if self._observer is not None:
            self._observer.join()

    def _set_status(
        self, step: str, current: int = 0, total: int = 0, state: str = "ingesting"
    ):
        self.status = {
            "state": state,
            "step": step,
            "ready": False,
            "current": current,
            "total": total,
        }

    def _set_idle(self):
        self.status = {
            "state": "idle",
            "step": "",
            "ready": True,
            "current": 0,
            "total": 0,
        }

    def _run_pipeline(self):
        """Run the fast ingestion pipeline (no LLM judging)."""
        from sessionlog.ingest import run_ingest
        from retro.sessions import build_sessions, build_tool_usage
        from retro.features import extract_features
        from retro.skills import assess_skills
        from retro.scoring import compute_scores
        from retro.intents import classify_all_intents
        from retro.baselines import compute_baselines
        from retro.prescriptions import generate_prescriptions

        n = 10
        self._set_status("Ingesting JSONL files", 1, n)
        run_ingest(source_specs=self._source_specs)
        self._set_status("Building sessions", 2, n)
        build_sessions()
        self._set_status("Analyzing tool usage", 3, n)
        build_tool_usage()
        self._set_status("Extracting features", 4, n)
        extract_features()
        self._set_status("Assessing skills", 5, n)
        assess_skills()
        self._set_status("Computing scores", 6, n)
        compute_scores()
        self._set_status("Classifying intents", 7, n)
        classify_all_intents()
        self._set_status("Computing baselines", 8, n)
        compute_baselines()
        self._set_status("Generating prescriptions", 9, n)
        generate_prescriptions()
        self._set_status("Building search index", 10, n)
        from sessionlog.db import rebuild_fts_index

        rebuild_fts_index()
        self._set_idle()

    def _run_full_refresh(self, concurrency: int = 12):
        """Run the full pipeline including LLM judging with progress."""
        from sessionlog.ingest import run_ingest
        from retro.sessions import build_sessions, build_tool_usage
        from retro.features import extract_features
        from retro.skills import assess_skills
        from retro.scoring import compute_scores
        from retro.intents import classify_all_intents
        from retro.baselines import compute_baselines
        from retro.prescriptions import generate_prescriptions
        from retro.llm_judge import judge_sessions

        # Phase 1: fast pipeline (9 steps)
        n = 9
        self._set_status("Ingesting JSONL files", 1, n)
        run_ingest(source_specs=self._source_specs)
        self._set_status("Building sessions", 2, n)
        build_sessions()
        self._set_status("Analyzing tool usage", 3, n)
        build_tool_usage()
        self._set_status("Extracting features", 4, n)
        extract_features()
        self._set_status("Assessing skills", 5, n)
        assess_skills()
        self._set_status("Computing scores", 6, n)
        compute_scores()
        self._set_status("Classifying intents", 7, n)
        classify_all_intents()
        self._set_status("Computing baselines", 8, n)
        compute_baselines()
        self._set_status("Generating prescriptions", 9, n)
        generate_prescriptions()

        # Phase 2: LLM judging (reports per-session progress)
        def on_judge_progress(done, total, ok, errors):
            self._set_status(
                f"Judging sessions ({ok} ok, {errors} errors)",
                current=done,
                total=total,
                state="judging",
            )

        self._set_status("Starting LLM judge", 0, 0, state="judging")
        judge_sessions(concurrency=concurrency, progress_callback=on_judge_progress)

        # Phase 3: recompute baselines/prescriptions with new judgments
        self._set_status("Recomputing baselines", 1, 2)
        compute_baselines()
        self._set_status("Regenerating prescriptions", 2, 2)
        generate_prescriptions()

        self._set_idle()
