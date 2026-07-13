from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import ALLOWED_TRANSITIONS, InvalidTransition, TaskRecord, TaskState, WorkspaceLeaseConflict, now


class TaskStore:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.artifacts = home / "artifacts"
        self.artifacts.mkdir(exist_ok=True)
        self.connection = sqlite3.connect(home / "recollectlines.db", timeout=5, check_same_thread=False)
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
            CREATE TABLE IF NOT EXISTS workspace_leases (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                canonical_source TEXT NOT NULL,
                worktree_path TEXT NOT NULL,
                branch TEXT NOT NULL,
                base_sha TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                released_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS workspace_leases_active_source
                ON workspace_leases(canonical_source) WHERE status = 'active';
            CREATE TABLE IF NOT EXISTS runtime_launches (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                adapter TEXT NOT NULL,
                adapter_label TEXT NOT NULL,
                pid INTEGER,
                pgid INTEGER,
                launched_at TEXT NOT NULL,
                command_json TEXT NOT NULL,
                workspace TEXT NOT NULL,
                events_artifact TEXT,
                stderr_artifact TEXT,
                reconciliation_marker TEXT NOT NULL DEFAULT 'unreconciled'
            );
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

    def write_artifact(self, task_id: str, name: str, content: str | bytes) -> Path:
        if "/" in name or name in {"", ".", "..", "manifest.json"}:
            raise ValueError("Artifact name must be a simple non-manifest filename")
        path = self.artifacts / task_id / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
        self.refresh_manifest(task_id)
        return path

    def acquire_lease(self, task_id: str, canonical_source: str, worktree_path: str, branch: str, base_sha: str) -> None:
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO workspace_leases "
                    "(task_id, canonical_source, worktree_path, branch, base_sha, status, created_at, released_at) "
                    "VALUES (?, ?, ?, ?, ?, 'active', ?, NULL)",
                    (task_id, canonical_source, worktree_path, branch, base_sha, now()),
                )
        except sqlite3.IntegrityError as error:
            raise WorkspaceLeaseConflict(
                f"Source workspace already has an active writer lease: {canonical_source}"
            ) from error

    def release_lease(self, task_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE workspace_leases SET status = 'released', released_at = ? WHERE task_id = ? AND status = 'active'",
                (now(), task_id),
            )

    def get_lease(self, task_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM workspace_leases WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def record_launch(
        self, task_id: str, *, adapter: str, adapter_label: str, pid: int, pgid: int,
        command: list[str], workspace: str, events_artifact: str | None, stderr_artifact: str | None,
    ) -> None:
        """Persist durable launch identity the moment an adapter process is actually spawned.

        `command` should already be redacted by the caller; this stores whatever it is given.
        """
        with self.connection:
            self.connection.execute(
                "INSERT INTO runtime_launches "
                "(task_id, adapter, adapter_label, pid, pgid, launched_at, command_json, workspace, events_artifact, stderr_artifact) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, adapter, adapter_label, pid, pgid, now(), json.dumps(command), workspace, events_artifact, stderr_artifact),
            )

    def get_launch(self, task_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM runtime_launches WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["command"] = json.loads(data.pop("command_json"))
        return data

    def mark_launch_reconciled(self, task_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE runtime_launches SET reconciliation_marker = 'reconciled' WHERE task_id = ?",
                (task_id,),
            )

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
