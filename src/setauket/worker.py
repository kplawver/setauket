"""Durable embedding work and catch-up archival in one background thread."""

import logging
import threading
import time

import sqlite_vec

from setauket.models import TEXT_REPO, LocalModels
from setauket.storage import Store

log = logging.getLogger(__name__)

# Compaction slices a session into 25-turn segments so each summarizer call sees a bounded
# transcript; whole-session map/combine passes degenerated on long sessions.
SEGMENT_TURNS = 25


class Worker:
    def __init__(self, store: Store, models: LocalModels, interval: float = 10):
        self.store, self.models, self.interval = store, models, interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="setauket-worker")

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=10)
        self.models.unload_text()

    def sweep(self, now: float | None = None):
        now = now or time.time()
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO jobs(kind,source_id,next_at) "
                       "SELECT 'archive',id,? FROM sessions WHERE archived_at IS NULL AND last_turn_at<?",
                       (now, now - 72 * 3600))

    def work_one(self) -> bool:
        with self.store.connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE next_at<=? ORDER BY CASE kind WHEN 'embed' THEN 0 ELSE 1 END,id LIMIT 1",
                             (time.time(),)).fetchone()
            if job is None:
                return False
            job = dict(job)
        try:
            if job["kind"] == "embed":
                with self.store.connect() as db:
                    chunk = db.execute("SELECT * FROM chunks WHERE id=?", (job["source_id"],)).fetchone()
                    chunk = dict(chunk) if chunk else None
                if chunk:
                    vector = self.models.embed(chunk["content"])
                    with self.store.connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        # A chunk might be deleted while inference is running.
                        if db.execute("SELECT 1 FROM chunks WHERE id=?", (chunk["id"],)).fetchone():
                            table = "vec_memory" if chunk["category"] == "memory" else "vec_history"
                            db.execute(f"INSERT OR REPLACE INTO {table}(rowid,embedding) VALUES (?,?)",
                                       (chunk["id"], sqlite_vec.serialize_float32(vector)))
                            db.execute("UPDATE chunks SET embedded=1 WHERE id=?", (chunk["id"],))
            elif job["kind"] == "archive":
                session = self.store.get_session(job["source_id"])
                if session and session["archived_at"] is None:
                    if session["last_turn_at"] > time.time() - 72 * 3600:
                        pass  # Received a new turn since the job was queued.
                    else:
                        try:
                            turns = session["turns"]
                            summaries = [self.models.summarize(turns[i:i + SEGMENT_TURNS])
                                         for i in range(0, len(turns), SEGMENT_TURNS)]
                            self.store.archive(session["id"], session["last_turn_at"], summaries, TEXT_REPO)
                        finally:
                            self.models.unload_text()
            else:
                raise ValueError("Unknown job type")
            with self.store.connect() as db:
                db.execute("DELETE FROM jobs WHERE id=?", (job["id"],))
        except Exception as error:  # noqa: BLE001 - retry unexpected failures without losing source data.
            log.warning("Background %s job failed: %s", job["kind"], type(error).__name__)
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET attempts=attempts+1,error=?,next_at=? WHERE id=?",
                           (str(error)[:300], time.time() + min(3600, 30 * (2 ** min(job["attempts"], 7))), job["id"]))
        return True

    def run(self):
        next_sweep = 0
        while not self.stop_event.is_set():
            if time.time() >= next_sweep:
                self.sweep()
                next_sweep = time.time() + 24 * 3600
            if not self.work_one():
                self.stop_event.wait(self.interval)
