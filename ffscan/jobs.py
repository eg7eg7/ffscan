"""Durable work queues in SQLite, so queued processing, AI analysis and uploads survive an app restart.

Each queued item is written to disk before it runs and marked done after; on startup, anything still pending is
handed back to the workers in the original order. SQLite is part of Python: no database server to run.
"""
import json
import queue
import sqlite3
import threading
import time


class JobStore:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', error TEXT, created REAL, finished REAL)""")
        self.lock = threading.Lock()
        self.queues = {}

    def queue(self, kind):
        """The in-memory queue workers read from; filled from disk on first use (resume after a restart)."""
        if kind not in self.queues:
            q = self.queues[kind] = queue.Queue()
            for job_id, payload in self.pending(kind):
                q.put((job_id, payload))
        return self.queues[kind]

    def put(self, kind, payload):
        with self.lock:
            job_id = self.db.execute("INSERT INTO jobs (kind, payload, created) VALUES (?, ?, ?)",
                                     (kind, json.dumps(payload), time.time())).lastrowid
        self.queue(kind).put((job_id, payload))
        return job_id

    def finish(self, job_id, error=None):
        with self.lock:
            self.db.execute("UPDATE jobs SET state = ?, error = ?, finished = ? WHERE id = ?",
                            ("failed" if error else "done", error, time.time(), job_id))

    def pending(self, kind):
        with self.lock:
            rows = self.db.execute("SELECT id, payload FROM jobs WHERE kind = ? AND state = 'pending' ORDER BY id",
                                   (kind,)).fetchall()
        return [(job_id, json.loads(payload)) for job_id, payload in rows]

    def counts(self):
        with self.lock:
            return dict(self.db.execute("SELECT kind, COUNT(*) FROM jobs WHERE state = 'pending' GROUP BY kind"))

    def prune(self, days=30):
        """Forget finished jobs older than `days` (pending ones are never removed)."""
        with self.lock:
            self.db.execute("DELETE FROM jobs WHERE state != 'pending' AND finished < ?", (time.time() - days * 86400,))
