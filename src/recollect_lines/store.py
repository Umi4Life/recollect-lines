from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import ALLOWED_TRANSITIONS, InvalidTransition, TaskRecord, TaskState, now


class TaskStore:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.artifacts = home / "artifacts"
        self.artifacts.mkdir(exist_ok=True)
        self.connection = sqlite3.connect(home / "recollect.db", timeout=5, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                workspace TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                profile TEXT NOT NULL,
                timeout_seconds INTEGER NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                timestamp TEXT NOT NULL,
                type TEXT NOT NULL,
                state_before TEXT,
                state_after TEXT,
                message TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_task_id_id ON events(task_id, id);
            """
        )
        self.connection.commit()

    def create(self, record: TaskRecord) -> TaskRecord:
        with self.connection:
            self.connection.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*record.json().values(),),
            )
            self.event(record.id, "task.created", None, record.state, "Task accepted", {})
        (self.artifacts / record.id).mkdir(exist_ok=True)
        return record

    def get(self, task_id: str) -> TaskRecord:
        row = self.connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        return TaskRecord(**{**dict(row), "state": TaskState(row["state"])})

    def list(self) -> list[TaskRecord]:
        rows = self.connection.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
        return [TaskRecord(**{**dict(row), "state": TaskState(row["state"])}) for row in rows]

    def active_count(self, profile: str) -> int:
        active = tuple(state.value for state in (TaskState.QUEUED, TaskState.PREPARING, TaskState.RUNNING, TaskState.COLLECTING, TaskState.CANCELLING))
        return self.connection.execute(
            f"SELECT COUNT(*) FROM tasks WHERE profile = ? AND state IN ({','.join('?' for _ in active)})",
            (profile, *active),
        ).fetchone()[0]

    def transition(self, task_id: str, target: TaskState, message: str, metadata: dict[str, Any] | None = None) -> TaskRecord:
        record = self.get(task_id)
        if target not in ALLOWED_TRANSITIONS.get(record.state, set()):
            raise InvalidTransition(f"Cannot transition {record.state.value} to {target.value}")
        timestamp = now()
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
                (target.value, timestamp, task_id),
            )
            self.event(task_id, f"task.{target.value}", record.state, target, message, metadata or {})
        return self.get(task_id)

    def event(self, task_id: str, event_type: str, before: TaskState | None, after: TaskState | None, message: str, metadata: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO events (task_id, timestamp, type, state_before, state_after, message, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, now(), event_type, before.value if before else None, after.value if after else None, message, json.dumps(metadata, sort_keys=True)),
        )

    def events(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM events WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return [{**dict(row), "metadata": json.loads(row["metadata_json"])} for row in rows]

    def write_artifact(self, task_id: str, name: str, content: str) -> Path:
        if "/" in name or name in {"", ".", "..", "manifest.json"}:
            raise ValueError("Artifact name must be a simple non-manifest filename")
        path = self.artifacts / task_id / name
        path.write_text(content)
        self.refresh_manifest(task_id)
        return path

    def refresh_manifest(self, task_id: str) -> Path:
        directory = self.artifacts / task_id
        if not directory.is_dir():
            raise KeyError(f"No artifact directory for task: {task_id}")
        files = []
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name == "manifest.json":
                continue
            payload = path.read_bytes()
            files.append({"name": path.name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
        manifest = {"task_id": task_id, "generated_at": now(), "retention": "manual_cleanup", "files": files}
        path = directory / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return path

    def artifact_manifest(self, task_id: str) -> dict[str, Any]:
        path = self.artifacts / task_id / "manifest.json"
        if not path.is_file():
            raise KeyError(f"No artifact manifest for task: {task_id}")
        return json.loads(path.read_text())
