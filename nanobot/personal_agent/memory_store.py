"""Host-owned project memory with explicit provenance and update deduplication."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

MAX_PROJECT_NAME = 120
MAX_PROJECT_DESCRIPTION = 1000
MAX_MEMORY_CONTENT = 2000
MAX_SEARCH_RESULTS = 8
MAX_MEMORY_CONTEXT_CHARS = 3000
_TOKEN = re.compile(r"[\w-]{2,}", re.UNICODE)
_SECRET = re.compile(r"(?i)(?:api[_ -]?key|access[_ -]?token|password|secret|private[_ -]?key|\bsk-[A-Za-z0-9_-]{12,})")


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    project_id: str | None
    project_name: str | None
    kind: str
    content: str
    source_type: str
    source_id: str
    task_id: str | None
    created_at: str
    updated_at: str
    deleted_at: str | None
    superseded_by: str | None


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    project_id: str
    name: str
    description: str
    source_type: str
    source_id: str
    created_at: str
    updated_at: str


class EntityType(StrEnum):
    PROJECT = "project"
    MEMORY = "memory"
    TASK = "task"
    FILE = "file"
    ARTIFACT = "artifact"


class RelationshipType(StrEnum):
    PROJECT_HAS_MEMORY = "PROJECT_HAS_MEMORY"
    TASK_BELONGS_TO_PROJECT = "TASK_BELONGS_TO_PROJECT"
    TASK_USES_FILE = "TASK_USES_FILE"
    TASK_PRODUCES_ARTIFACT = "TASK_PRODUCES_ARTIFACT"
    TASK_CONTINUES_TASK = "TASK_CONTINUES_TASK"
    MEMORY_SUPERSEDES_MEMORY = "MEMORY_SUPERSEDES_MEMORY"
    TASK_USES_MEMORY = "TASK_USES_MEMORY"


class GraphState(StrEnum):
    CURRENT = "current"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class GraphEntity:
    entity_id: str
    entity_type: EntityType
    external_id: str
    label: str
    state: GraphState
    source_type: str
    source_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class GraphRelationship:
    relationship_id: str
    source_entity_id: str
    relationship_type: RelationshipType
    target_entity_id: str
    state: GraphState
    source_type: str
    source_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class GraphSearchResult:
    matched: tuple[GraphEntity, ...]
    connected: tuple[GraphEntity, ...]
    relationships: tuple[GraphRelationship, ...]
    source_ids: tuple[str, ...]


class MemoryStore:
    """Persist explicit owner memories separately from task and model histories."""

    def __init__(self, path: Path, *, read_only: bool = False):
        if not path.is_absolute():
            raise ValueError("memory database path must be absolute")
        self.path = path
        self._read_only = read_only
        if read_only:
            if self.path.is_symlink() or not self.path.is_file() or self.path.parent.is_symlink():
                raise ValueError("read-only memory database must be an existing regular file")
            if self.path.stat().st_uid != os.getuid() or stat.S_IMODE(self.path.stat().st_mode) & 0o077:
                raise ValueError("read-only memory database must be owner-only and owned by this user")
            if self.path.parent.stat().st_uid != os.getuid() or stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
                raise ValueError("read-only memory database directory must be owner-only")
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or not self.path.parent.is_dir() or self.path.is_symlink():
            raise ValueError("memory database must be in a private real directory")
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise ValueError("memory database directory must be owner-only")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink():
            raise RuntimeError("memory database must not be a symlink")
        database: str | Path = f"{self.path.as_uri()}?mode=ro" if self._read_only else self.path
        connection = sqlite3.connect(database, timeout=10, isolation_level=None, uri=self._read_only)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if self._read_only:
            connection.execute("PRAGMA query_only=ON")
        else:
            connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    normalized_name TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    memory_id TEXT PRIMARY KEY,
                    project_id TEXT REFERENCES projects(project_id),
                    kind TEXT NOT NULL CHECK(kind IN ('fact','preference','decision','task_reference')),
                    content TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    task_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    superseded_by TEXT REFERENCES memories(memory_id)
                );
                CREATE TABLE IF NOT EXISTS memory_references (
                    memory_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE CASCADE,
                    reference_type TEXT NOT NULL CHECK(reference_type IN ('task','file','artifact')),
                    reference_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    PRIMARY KEY(memory_id, reference_type, reference_id)
                );
                CREATE TABLE IF NOT EXISTS memory_message_context (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    memory_id TEXT NOT NULL REFERENCES memories(memory_id),
                    PRIMARY KEY(chat_id, message_id, memory_id)
                );
                CREATE TABLE IF NOT EXISTS memory_task_context (
                    task_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL REFERENCES memories(memory_id),
                    source_update_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, memory_id)
                );
                CREATE TABLE IF NOT EXISTS memory_updates (
                    source_update_id INTEGER PRIMARY KEY,
                    operation TEXT NOT NULL,
                    result_id TEXT,
                    response TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_tombstones (
                    memory_id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    deleted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS graph_entities (
                    entity_id TEXT PRIMARY KEY,
                    entity_type TEXT NOT NULL CHECK(entity_type IN ('project','memory','task','file','artifact')),
                    external_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('current','superseded','deleted')),
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, external_id)
                );
                CREATE TABLE IF NOT EXISTS graph_relationships (
                    relationship_id TEXT PRIMARY KEY,
                    source_entity_id TEXT NOT NULL REFERENCES graph_entities(entity_id),
                    relationship_type TEXT NOT NULL CHECK(relationship_type IN (
                        'PROJECT_HAS_MEMORY','TASK_BELONGS_TO_PROJECT','TASK_USES_FILE',
                        'TASK_PRODUCES_ARTIFACT','TASK_CONTINUES_TASK','MEMORY_SUPERSEDES_MEMORY','TASK_USES_MEMORY'
                    )),
                    target_entity_id TEXT NOT NULL REFERENCES graph_entities(entity_id),
                    state TEXT NOT NULL CHECK(state IN ('current','superseded','deleted')),
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_entity_id, relationship_type, target_entity_id)
                );
                CREATE INDEX IF NOT EXISTS memory_project_idx ON memories(project_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS memory_task_idx ON memories(task_id);
                CREATE INDEX IF NOT EXISTS memory_active_idx ON memories(deleted_at, superseded_by, updated_at DESC);
                CREATE INDEX IF NOT EXISTS graph_label_idx ON graph_entities(state, label);
                CREATE INDEX IF NOT EXISTS graph_outgoing_idx ON graph_relationships(source_entity_id, state);
                CREATE INDEX IF NOT EXISTS graph_incoming_idx ON graph_relationships(target_entity_id, state);"""
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._migrate_graph(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        os.chmod(self.path, 0o600)

    def _migrate_graph(self, connection: sqlite3.Connection) -> None:
        """Build graph indexes from existing authoritative memory rows without rewriting them."""
        now = _now()
        for row in connection.execute("SELECT * FROM projects"):
            self._put_entity(
                connection, EntityType.PROJECT, str(row["project_id"]), str(row["name"]),
                GraphState.CURRENT, str(row["source_type"]), str(row["source_id"]),
                str(row["created_at"]), str(row["updated_at"]),
            )
        memory_rows = connection.execute("SELECT * FROM memories").fetchall()
        by_id = {str(row["memory_id"]): row for row in memory_rows}
        for row in memory_rows:
            memory_id = str(row["memory_id"])
            state = _memory_state(row)
            entity_id = self._put_entity(
                connection, EntityType.MEMORY, memory_id, f"{row['kind']} memory {memory_id}",
                state, str(row["source_type"]), str(row["source_id"]),
                str(row["created_at"]), str(row["updated_at"]),
            )
            project_id = row["project_id"]
            if project_id:
                project_entity = f"project:{project_id}"
                self._put_relationship(
                    connection, project_entity, RelationshipType.PROJECT_HAS_MEMORY, entity_id,
                    state, str(row["source_type"]), str(row["source_id"]), str(row["created_at"]),
                )
            newer_id = row["superseded_by"]
            if newer_id:
                newer = by_id.get(str(newer_id))
                newer_source_type = str(newer["source_type"]) if newer else str(row["source_type"])
                newer_source_id = str(newer["source_id"]) if newer else str(row["source_id"])
                newer_created = str(newer["created_at"]) if newer else str(row["updated_at"])
                newer_entity = self._put_entity(
                    connection, EntityType.MEMORY, str(newer_id), "memory", GraphState.CURRENT,
                    newer_source_type, newer_source_id, newer_created, now,
                )
                self._put_relationship(
                    connection, newer_entity, RelationshipType.MEMORY_SUPERSEDES_MEMORY,
                    entity_id, GraphState.CURRENT, newer_source_type, newer_source_id, newer_created,
                )
        for row in connection.execute("SELECT * FROM memory_tombstones"):
            self._put_entity(
                connection, EntityType.MEMORY, str(row["memory_id"]), "deleted memory",
                GraphState.DELETED, str(row["source_type"]), str(row["source_id"]),
                str(row["deleted_at"]), str(row["deleted_at"]),
            )
        for row in connection.execute("SELECT * FROM memory_references"):
            memory = by_id.get(str(row["memory_id"]))
            if memory is None:
                continue
            state = _memory_state(memory)
            self._index_reference(connection, memory, row, state)
        for row in connection.execute("SELECT * FROM memory_task_context"):
            memory = by_id.get(str(row["memory_id"]))
            if memory is None or _memory_state(memory) is not GraphState.CURRENT:
                continue
            task_id = str(row["task_id"])
            task = self._put_entity(
                connection, EntityType.TASK, task_id, task_id, GraphState.CURRENT,
                "task_store", task_id, str(row["created_at"]), str(row["created_at"]),
            )
            memory_entity = self._put_entity(
                connection, EntityType.MEMORY, str(memory["memory_id"]), f"{memory['kind']} memory {memory['memory_id']}",
                GraphState.CURRENT, str(memory["source_type"]), str(memory["source_id"]),
                str(memory["created_at"]), str(memory["updated_at"]),
            )
            self._put_relationship(
                connection, task, RelationshipType.TASK_USES_MEMORY, memory_entity,
                GraphState.CURRENT, "telegram_owner", str(row["source_update_id"]), str(row["created_at"]),
            )
            if memory["project_id"]:
                project = f"project:{memory['project_id']}"
                self._put_relationship(
                    connection, task, RelationshipType.TASK_BELONGS_TO_PROJECT, project,
                    GraphState.CURRENT, "telegram_owner", str(row["source_update_id"]), str(row["created_at"]),
                )

    def _index_reference(
        self, connection: sqlite3.Connection, memory: sqlite3.Row, reference: sqlite3.Row,
        state: GraphState,
    ) -> None:
        ref_type = str(reference["reference_type"])
        ref_id = str(reference["reference_id"])
        source_id = str(memory["task_id"] or memory["source_id"])
        provenance = "task_store" if memory["task_id"] else str(memory["source_type"])
        created = str(memory["created_at"])
        if ref_type == "task":
            task_entity = self._put_entity(
                connection, EntityType.TASK, ref_id, ref_id, GraphState.CURRENT, "task_store", ref_id, created, created,
            )
            if memory["project_id"]:
                project_entity = f"project:{memory['project_id']}"
                self._put_relationship(
                    connection, task_entity, RelationshipType.TASK_BELONGS_TO_PROJECT, project_entity,
                    state, provenance, source_id, created,
                )
            return
        entity_type = EntityType.FILE if ref_type == "file" else EntityType.ARTIFACT
        file_entity = self._put_entity(
            connection, entity_type, ref_id, str(reference["label"]), GraphState.CURRENT,
            "task_store", source_id, created, created,
        )
        task_id = str(memory["task_id"] or "")
        if task_id:
            task_entity = self._put_entity(
                connection, EntityType.TASK, task_id, task_id, GraphState.CURRENT,
                "task_store", task_id, created, created,
            )
            relation = RelationshipType.TASK_USES_FILE if entity_type is EntityType.FILE else RelationshipType.TASK_PRODUCES_ARTIFACT
            self._put_relationship(
                connection, task_entity, relation, file_entity, GraphState.CURRENT, provenance, source_id, created,
            )

    def _refresh_memory_graph(self, connection: sqlite3.Connection, memory_id: str) -> None:
        row = connection.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
        if row is None:
            return
        state = _memory_state(row)
        now = str(row["updated_at"])
        memory_entity = self._put_entity(
            connection, EntityType.MEMORY, memory_id, f"{row['kind']} memory {memory_id}", state,
            str(row["source_type"]), str(row["source_id"]), str(row["created_at"]), now,
        )
        if row["project_id"]:
            project_row = connection.execute("SELECT * FROM projects WHERE project_id=?", (row["project_id"],)).fetchone()
            if project_row:
                project_entity = self._put_entity(
                    connection, EntityType.PROJECT, str(project_row["project_id"]), str(project_row["name"]),
                    GraphState.CURRENT, str(project_row["source_type"]), str(project_row["source_id"]),
                    str(project_row["created_at"]), str(project_row["updated_at"]),
                )
                self._put_relationship(
                    connection, project_entity, RelationshipType.PROJECT_HAS_MEMORY, memory_entity,
                    state, str(row["source_type"]), str(row["source_id"]), str(row["created_at"]),
                )
        for reference in connection.execute("SELECT * FROM memory_references WHERE memory_id=?", (memory_id,)):
            self._index_reference(connection, row, reference, state)

    @staticmethod
    def _put_entity(
        connection: sqlite3.Connection, entity_type: EntityType, external_id: str, label: str,
        state: GraphState, source_type: str, source_id: str, created_at: str, updated_at: str,
    ) -> str:
        entity_id = f"{entity_type.value}:{external_id}"
        connection.execute(
            "INSERT INTO graph_entities VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(entity_type,external_id) DO UPDATE SET label=excluded.label,state=excluded.state,"
            "source_type=excluded.source_type,source_id=excluded.source_id,updated_at=excluded.updated_at",
            (entity_id, entity_type.value, external_id, label[:240], state.value, source_type, source_id, created_at, updated_at),
        )
        return entity_id

    @staticmethod
    def _put_relationship(
        connection: sqlite3.Connection, source_id: str, relation: RelationshipType, target_id: str,
        state: GraphState, provenance_type: str, provenance_id: str, created_at: str,
        updated_at: str | None = None,
    ) -> None:
        relationship_id = hashlib.sha256(f"{source_id}\0{relation.value}\0{target_id}".encode()).hexdigest()
        updated_at = updated_at or created_at
        connection.execute(
            "INSERT INTO graph_relationships VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_entity_id,relationship_type,target_entity_id) DO UPDATE SET "
            "state=excluded.state,source_type=excluded.source_type,source_id=excluded.source_id,"
            "updated_at=MAX(graph_relationships.updated_at,excluded.updated_at)",
            (relationship_id, source_id, relation.value, target_id, state.value, provenance_type, provenance_id, created_at, updated_at),
        )

    def create_project(self, update_id: int, name: str, description: str = "") -> tuple[ProjectRecord | None, str, bool]:
        name = _validate_text(name, MAX_PROJECT_NAME, "project name")
        description = _validate_text(description, MAX_PROJECT_DESCRIPTION, "project description", allow_empty=True)
        if _SECRET.search(description):
            raise ValueError("Project descriptions cannot contain credentials or secret material")
        now = _now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = self._prior_update(connection, update_id)
            if prior:
                connection.commit()
                return self.get_project_by_name(name), prior["response"], False
            normalized = _normalize(name)
            row = connection.execute("SELECT project_id FROM projects WHERE normalized_name=?", (normalized,)).fetchone()
            if row:
                project_id = str(row["project_id"])
                connection.execute(
                    "UPDATE projects SET name=?, description=CASE WHEN ?='' THEN description ELSE ? END, source_type='telegram_owner', source_id=?, updated_at=? WHERE project_id=?",
                    (name, description, description, str(update_id), now, project_id),
                )
                response = f"Project {name} was updated." if description else f"Project {name} is ready; its description was left unchanged."
            else:
                project_id = "project-" + secrets.token_hex(12)
                connection.execute(
                    "INSERT INTO projects VALUES (?, ?, ?, ?, 'telegram_owner', ?, ?, ?)",
                    (project_id, normalized, name, description, str(update_id), now, now),
                )
                response = f"Project {name} was saved."
            connection.execute(
                "INSERT INTO memory_updates VALUES (?, 'project', ?, ?, ?)",
                (update_id, project_id, response, now),
            )
            project_row = connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
            self._put_entity(
                connection, EntityType.PROJECT, project_id, name, GraphState.CURRENT,
                "telegram_owner", str(update_id), str(project_row["created_at"]), now,
            )
            connection.commit()
        return self.get_project_by_name(name), response, True

    def remember(
        self, update_id: int, content: str, *, project_name: str | None = None, kind: str = "fact"
    ) -> tuple[str | None, str, bool]:
        content = _validate_text(content, MAX_MEMORY_CONTENT, "memory")
        if _SECRET.search(content):
            raise ValueError("Memory cannot contain credentials or secret material")
        if kind not in {"fact", "preference", "decision"}:
            raise ValueError("unsupported memory type")
        project_name = _validate_text(project_name, MAX_PROJECT_NAME, "project name") if project_name else None
        now = _now()
        memory_id = "memory-" + secrets.token_hex(12)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = self._prior_update(connection, update_id)
            if prior:
                connection.commit()
                return prior["result_id"], prior["response"], False
            project_id = self._ensure_project(connection, project_name, update_id, now) if project_name else None
            connection.execute(
                "INSERT INTO memories(memory_id, project_id, kind, content, source_type, source_id, created_at, updated_at) VALUES (?, ?, ?, ?, 'telegram_owner', ?, ?, ?)",
                (memory_id, project_id, kind, content, str(update_id), now, now),
            )
            response = f"Saved {kind} memory {memory_id}" + (f" under {project_name}." if project_name else ".")
            connection.execute("INSERT INTO memory_updates VALUES (?, 'remember', ?, ?, ?)", (update_id, memory_id, response, now))
            self._refresh_memory_graph(connection, memory_id)
            connection.commit()
        return memory_id, response, True

    def remember_task(
        self, update_id: int, project_name: str, *, task_id: str, summary: str, references: tuple[tuple[str, str, str], ...]
    ) -> tuple[str | None, str, bool]:
        project_name = _validate_text(project_name, MAX_PROJECT_NAME, "project name")
        summary = _validate_text(summary, MAX_MEMORY_CONTENT, "task summary")
        now = _now()
        memory_id = "memory-" + secrets.token_hex(12)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = self._prior_update(connection, update_id)
            if prior:
                connection.commit()
                return prior["result_id"], prior["response"], False
            project_id = self._ensure_project(connection, project_name, update_id, now)
            connection.execute(
                "INSERT INTO memories(memory_id, project_id, kind, content, source_type, source_id, task_id, created_at, updated_at) VALUES (?, ?, 'task_reference', ?, 'task_store', ?, ?, ?, ?)",
                (memory_id, project_id, summary, task_id, task_id, now, now),
            )
            connection.execute(
                "INSERT OR IGNORE INTO memory_references(memory_id, reference_type, reference_id, label) VALUES (?, 'task', ?, ?)",
                (memory_id, task_id, "Saved task result"),
            )
            for reference_type, reference_id, label in references[:20]:
                if reference_type not in {"file", "artifact"} or not reference_id or len(reference_id) > 512:
                    continue
                connection.execute(
                    "INSERT OR IGNORE INTO memory_references(memory_id, reference_type, reference_id, label) VALUES (?, ?, ?, ?)",
                    (memory_id, reference_type, reference_id, label[:180]),
                )
            response = f"Saved a reference to task {task_id} under project {project_name} as {memory_id}. The task result and files remain in their original host records."
            connection.execute("INSERT INTO memory_updates VALUES (?, 'save_task', ?, ?, ?)", (update_id, memory_id, response, now))
            self._refresh_memory_graph(connection, memory_id)
            connection.commit()
        return memory_id, response, True

    def correct(self, update_id: int, memory_id: str, replacement: str) -> tuple[str | None, str, bool]:
        replacement = _validate_text(replacement, MAX_MEMORY_CONTENT, "replacement memory")
        if _SECRET.search(replacement):
            raise ValueError("Memory cannot contain credentials or secret material")
        now = _now()
        new_id = "memory-" + secrets.token_hex(12)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = self._prior_update(connection, update_id)
            if prior:
                connection.commit()
                return prior["result_id"], prior["response"], False
            old = connection.execute(
                "SELECT project_id, kind FROM memories WHERE memory_id=? AND deleted_at IS NULL AND superseded_by IS NULL",
                (memory_id,),
            ).fetchone()
            if old is None:
                response = "I could not find an active memory with that ID. Nothing changed."
                connection.execute("INSERT INTO memory_updates VALUES (?, 'correct', NULL, ?, ?)", (update_id, response, now))
                connection.commit()
                return None, response, True
            connection.execute(
                "INSERT INTO memories(memory_id, project_id, kind, content, source_type, source_id, created_at, updated_at) VALUES (?, ?, ?, ?, 'telegram_owner', ?, ?, ?)",
                (new_id, old["project_id"], old["kind"], replacement, str(update_id), now, now),
            )
            connection.execute("UPDATE memories SET superseded_by=?, updated_at=? WHERE memory_id=?", (new_id, now, memory_id))
            response = f"Updated memory. The newer version is {new_id}; the older record is retained as superseded history."
            connection.execute("INSERT INTO memory_updates VALUES (?, 'correct', ?, ?, ?)", (update_id, new_id, response, now))
            self._refresh_memory_graph(connection, memory_id)
            self._refresh_memory_graph(connection, new_id)
            old_entity_id = f"memory:{memory_id}"
            new_entity_id = f"memory:{new_id}"
            self._put_relationship(
                connection, new_entity_id, RelationshipType.MEMORY_SUPERSEDES_MEMORY,
                old_entity_id, GraphState.CURRENT, "telegram_owner", str(update_id), now,
            )
            connection.commit()
        return new_id, response, True

    def forget(self, update_id: int, memory_id: str) -> tuple[str, bool]:
        now = _now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = self._prior_update(connection, update_id)
            if prior:
                connection.commit()
                return prior["response"], False
            row = connection.execute(
                "SELECT memory_id, source_type, source_id FROM memories WHERE memory_id=? AND deleted_at IS NULL AND superseded_by IS NULL",
                (memory_id,),
            ).fetchone()
            if row is None:
                response = "I could not find an active memory with that ID. Nothing changed."
                result_id = None
            else:
                lineage = connection.execute(
                    "WITH RECURSIVE ids(memory_id) AS (SELECT ? UNION SELECT m.memory_id FROM memories m JOIN ids ON m.superseded_by=ids.memory_id) "
                    "SELECT m.memory_id,m.source_type,m.source_id FROM memories m JOIN ids USING(memory_id)",
                    (memory_id,),
                ).fetchall()
                ids = [str(item["memory_id"]) for item in lineage]
                task_project_pairs: set[tuple[str, str]] = set()
                for item in lineage:
                    old_id = str(item["memory_id"])
                    project_row = connection.execute(
                        "SELECT project_id,task_id FROM memories WHERE memory_id=?", (old_id,)
                    ).fetchone()
                    if project_row and project_row["project_id"]:
                        if project_row["task_id"]:
                            task_project_pairs.add((str(project_row["task_id"]), str(project_row["project_id"])))
                        for task_row in connection.execute(
                            "SELECT task_id FROM memory_task_context WHERE memory_id=?", (old_id,)
                        ):
                            task_project_pairs.add((str(task_row["task_id"]), str(project_row["project_id"])))
                    connection.execute(
                        "UPDATE graph_entities SET state='deleted',updated_at=? WHERE entity_id=?",
                        (now, f"memory:{old_id}"),
                    )
                    connection.execute(
                        "UPDATE graph_relationships SET state='deleted',updated_at=? "
                        "WHERE source_entity_id=? OR target_entity_id=?",
                        (now, f"memory:{old_id}", f"memory:{old_id}"),
                    )
                    connection.execute("DELETE FROM memory_message_context WHERE memory_id=?", (old_id,))
                    connection.execute("DELETE FROM memory_references WHERE memory_id=?", (old_id,))
                    connection.execute("DELETE FROM memory_task_context WHERE memory_id=?", (old_id,))
                    connection.execute(
                        "INSERT INTO memory_tombstones VALUES (?, ?, ?, ?)",
                        (old_id, str(item["source_type"]), str(item["source_id"]), now),
                    )
                connection.execute(f"DELETE FROM memories WHERE memory_id IN ({','.join('?' for _ in ids)})", ids)
                for task_id, project_id in task_project_pairs:
                    remaining = connection.execute(
                        "SELECT 1 FROM memories WHERE task_id=? AND project_id=? AND kind='task_reference' "
                        "AND deleted_at IS NULL AND superseded_by IS NULL UNION ALL "
                        "SELECT 1 FROM memory_task_context c JOIN memories m USING(memory_id) "
                        "WHERE c.task_id=? AND m.project_id=? AND m.deleted_at IS NULL AND m.superseded_by IS NULL LIMIT 1",
                        (task_id, project_id, task_id, project_id),
                    ).fetchone()
                    if remaining is None:
                        connection.execute(
                            "UPDATE graph_relationships SET state='deleted',updated_at=? WHERE "
                            "source_entity_id=? AND relationship_type=? AND target_entity_id=?",
                            (now, f"task:{task_id}", RelationshipType.TASK_BELONGS_TO_PROJECT.value, f"project:{project_id}"),
                        )
                response = f"Forgot memory {memory_id}. It is excluded from future retrieval."
                result_id = memory_id
            connection.execute("INSERT INTO memory_updates VALUES (?, 'forget', ?, ?, ?)", (update_id, result_id, response, now))
            connection.commit()
        return response, True

    def bind_message(self, chat_id: int, message_id: int, memory_ids: tuple[str, ...]) -> None:
        with closing(self._connect()) as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO memory_message_context(chat_id,message_id,memory_id) VALUES (?,?,?)",
                [(chat_id, message_id, memory_id) for memory_id in memory_ids],
            )

    def link_memories_to_task(self, update_id: int, task_id: str, memory_ids: tuple[str, ...]) -> None:
        now = _now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = self._prior_update(connection, update_id)
            if prior:
                connection.commit()
                return
            connection.executemany(
                "INSERT OR IGNORE INTO memory_task_context VALUES (?, ?, ?, ?)",
                [(task_id, memory_id, update_id, now) for memory_id in memory_ids],
            )
            task_entity = self._put_entity(
                connection, EntityType.TASK, task_id, task_id, GraphState.CURRENT,
                "task_store", task_id, now, now,
            )
            for memory_id in memory_ids[:20]:
                memory = connection.execute(
                    "SELECT * FROM memories WHERE memory_id=? AND deleted_at IS NULL AND superseded_by IS NULL",
                    (memory_id,),
                ).fetchone()
                if memory is None:
                    continue
                memory_entity = self._put_entity(
                    connection, EntityType.MEMORY, memory_id, f"{memory['kind']} memory {memory_id}",
                    GraphState.CURRENT, str(memory["source_type"]), str(memory["source_id"]),
                    str(memory["created_at"]), str(memory["updated_at"]),
                )
                self._put_relationship(
                    connection, task_entity, RelationshipType.TASK_USES_MEMORY, memory_entity,
                    GraphState.CURRENT, "telegram_owner", str(update_id), now, updated_at=now,
                )
                if memory["project_id"]:
                    self._put_relationship(
                        connection, task_entity, RelationshipType.TASK_BELONGS_TO_PROJECT,
                        f"project:{memory['project_id']}", GraphState.CURRENT,
                        "telegram_owner", str(update_id), now, updated_at=now,
                    )
            connection.execute(
                "INSERT INTO memory_updates VALUES (?, 'associate_task_context', ?, ?, ?)",
                (update_id, task_id, "Project memory context associated with queued task.", now),
            )
            connection.commit()

    def project_names_for_memories(self, memory_ids: tuple[str, ...]) -> tuple[str, ...]:
        if not memory_ids:
            return ()
        placeholders = ",".join("?" for _ in memory_ids)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT DISTINCT p.name FROM memories m JOIN projects p USING(project_id) WHERE m.memory_id IN ({placeholders}) AND m.deleted_at IS NULL AND m.superseded_by IS NULL",
                memory_ids,
            ).fetchall()
        return tuple(str(row["name"]) for row in rows)

    def project_names(self) -> tuple[str, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT name FROM projects ORDER BY updated_at DESC LIMIT 20").fetchall()
        return tuple(str(row["name"]) for row in rows)

    def project_names_for_message(self, chat_id: int, message_id: int) -> tuple[str, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT p.name FROM memory_message_context c JOIN memories m USING(memory_id) "
                "JOIN projects p USING(project_id) WHERE c.chat_id=? AND c.message_id=? "
                "AND m.deleted_at IS NULL AND m.superseded_by IS NULL", (chat_id, message_id)
            ).fetchall()
        return tuple(str(row["name"]) for row in rows)

    def memory_for_message(self, chat_id: int, message_id: int) -> tuple[MemoryRecord, ...]:
        with closing(self._connect()) as connection:
            ids = connection.execute(
                "SELECT memory_id FROM memory_message_context WHERE chat_id=? AND message_id=?", (chat_id, message_id)
            ).fetchall()
        records = [self.get(str(row["memory_id"])) for row in ids]
        return tuple(record for record in records if record is not None and record.deleted_at is None and record.superseded_by is None)

    def get(self, memory_id: str) -> MemoryRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT m.*, p.name AS project_name FROM memories m LEFT JOIN projects p USING(project_id) WHERE memory_id=?",
                (memory_id,),
            ).fetchone()
        return _memory_record(row) if row else None

    def get_project_by_name(self, name: str) -> ProjectRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM projects WHERE normalized_name=?", (_normalize(name),)).fetchone()
        return _project_record(row) if row else None

    def get_project(self, project_id: str) -> ProjectRecord | None:
        if not re.fullmatch(r"project-[0-9a-f]{24}", project_id):
            return None
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        return _project_record(row) if row else None

    def graph_entities(self, *, limit: int = 500) -> tuple[GraphEntity, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("graph entity limit must be between 1 and 500")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM graph_entities WHERE state IN ('current','superseded') ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(_graph_entity(row) for row in rows)

    def graph_entity(self, entity_id: str) -> GraphEntity | None:
        if len(entity_id) > 200 or not re.fullmatch(r"(?:project|memory|task|file|artifact):[^\x00-\x20]{1,180}", entity_id):
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM graph_entities WHERE entity_id=? AND state IN ('current','superseded')",
                (entity_id,),
            ).fetchone()
        return _graph_entity(row) if row else None

    def graph_expand(self, entity_id: str, *, max_nodes: int = 24) -> GraphSearchResult:
        """Return one node and a bounded set of adjacent current relationships."""
        if not 1 <= max_nodes <= 64:
            raise ValueError("graph expansion limit must be between 1 and 64")
        with closing(self._connect()) as connection:
            root_row = connection.execute(
                "SELECT * FROM graph_entities WHERE entity_id=? AND state IN ('current','superseded')",
                (entity_id,),
            ).fetchone()
            if root_row is None:
                return GraphSearchResult((), (), (), ())
            root = _graph_entity(root_row)
            rows = connection.execute(
                "SELECT * FROM graph_relationships WHERE state='current' AND "
                "(source_entity_id=? OR target_entity_id=?) ORDER BY created_at DESC LIMIT ?",
                (entity_id, entity_id, max_nodes * 2),
            ).fetchall()
            edges: list[GraphRelationship] = []
            neighbors: dict[str, GraphEntity] = {}
            for raw_edge in rows:
                edge = _graph_relationship(raw_edge)
                edges.append(edge)
                neighbor_id = edge.target_entity_id if edge.source_entity_id == entity_id else edge.source_entity_id
                if neighbor_id in neighbors or len(neighbors) >= max_nodes:
                    continue
                related = connection.execute(
                    "SELECT * FROM graph_entities WHERE entity_id=? AND "
                    "(state='current' OR (state='superseded' AND ?=1))",
                    (neighbor_id, int(edge.relationship_type is RelationshipType.MEMORY_SUPERSEDES_MEMORY)),
                ).fetchone()
                if related is not None:
                    neighbors[neighbor_id] = _graph_entity(related)
        entities = (root, *neighbors.values())
        source_ids = tuple(dict.fromkeys(
            [entity.source_id for entity in entities] + [edge.source_id for edge in edges]
        ))
        return GraphSearchResult((root,), tuple(neighbors.values()), tuple(edges), source_ids)

    def graph_history(self, memory_id: str) -> tuple[MemoryRecord, ...]:
        if not re.fullmatch(r"memory-[0-9a-f]{24}", memory_id):
            return ()
        with closing(self._connect()) as connection:
            root = connection.execute(
                "SELECT project_id FROM memories WHERE memory_id=? AND deleted_at IS NULL", (memory_id,)
            ).fetchone()
            if root is None:
                return ()
            rows = connection.execute(
                "SELECT m.*,p.name AS project_name FROM memories m LEFT JOIN projects p USING(project_id) "
                "WHERE m.project_id IS ? AND m.deleted_at IS NULL ORDER BY m.created_at LIMIT 500",
                (root["project_id"],),
            ).fetchall()
        converted = tuple(_memory_record(row) for row in rows)
        records = {record.memory_id: record for record in converted}
        lineage = {memory_id}
        for _ in range(16):
            expanded = set(lineage)
            for record in records.values():
                if record.memory_id in lineage or record.superseded_by in lineage:
                    expanded.add(record.memory_id)
                    if record.superseded_by in records:
                        expanded.add(record.superseded_by)
            if expanded == lineage:
                break
            lineage = expanded
        return tuple(sorted((records[key] for key in lineage if key in records), key=lambda item: item.created_at))

    def search(self, query: str, *, project_name: str | None = None, limit: int = MAX_SEARCH_RESULTS) -> tuple[MemoryRecord, ...]:
        tokens = set(_tokens(query))
        if not tokens:
            return ()
        where = "m.deleted_at IS NULL AND m.superseded_by IS NULL"
        parameters: list[object] = []
        if project_name:
            where += " AND p.normalized_name=?"
            parameters.append(_normalize(project_name))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT m.*, p.name AS project_name FROM memories m LEFT JOIN projects p USING(project_id) WHERE {where} ORDER BY m.updated_at DESC LIMIT 200",
                parameters,
            ).fetchall()
            ref_rows = connection.execute(
                "SELECT r.memory_id, r.label FROM memory_references r JOIN memories m USING(memory_id) WHERE m.deleted_at IS NULL AND m.superseded_by IS NULL"
            ).fetchall()
        reference_text: dict[str, str] = {}
        for row in ref_rows:
            reference_text.setdefault(str(row["memory_id"]), "")
            reference_text[str(row["memory_id"])] += " " + str(row["label"])
        scored: list[tuple[int, str, MemoryRecord]] = []
        for row in rows:
            record = _memory_record(row)
            haystack = set(_tokens(" ".join((record.project_name or "", record.content, reference_text.get(record.memory_id, "")))) )
            score = len(tokens & haystack)
            if score:
                scored.append((score, record.updated_at, record))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return tuple(item[2] for item in scored[:max(1, min(limit, MAX_SEARCH_RESULTS))])

    def project_context(self, project_name: str, *, limit: int = 10) -> tuple[str, tuple[MemoryRecord, ...]]:
        project = self.get_project_by_name(project_name)
        if project is None:
            return "", ()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT m.*, p.name AS project_name FROM memories m JOIN projects p USING(project_id) "
                "WHERE p.project_id=? AND m.deleted_at IS NULL AND m.superseded_by IS NULL "
                "ORDER BY m.updated_at DESC LIMIT ?", (project.project_id, max(1, min(limit, 20)))
            ).fetchall()
        records = tuple(_memory_record(row) for row in rows)
        lines = [
            f"Project: {project.name} [{project.project_id}; source {project.source_type}:{project.source_id}; "
            f"updated {project.updated_at}]"
        ]
        if project.description:
            lines.append(f"Description: {project.description}")
        for record in records:
            lines.append(f"{record.kind} [{record.memory_id}; source {record.source_type}:{record.source_id}]: {record.content}")
            refs = self.references(record.memory_id)
            labels = [label for kind, _, label in refs if kind in {"file", "artifact"}]
            if record.task_id:
                lines.append(f"Task reference: {record.task_id}")
            if labels:
                lines.append("File references: " + ", ".join(labels[:12]))
        context = "\n".join(lines)[:MAX_MEMORY_CONTEXT_CHARS]
        return context, records

    def sync_task(
        self,
        task_id: str,
        *,
        parent_task_id: str | None,
        files: tuple[tuple[str, str], ...],
        artifacts: tuple[tuple[str, str], ...],
        created_at: str,
    ) -> None:
        """Index bounded task/file metadata by stable references; TaskStore stays authoritative."""
        if not re.fullmatch(r"task-[0-9a-f]{32}", task_id):
            raise ValueError("invalid task ID for graph indexing")
        now = _now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            task_entity = self._put_entity(
                connection, EntityType.TASK, task_id, task_id, GraphState.CURRENT,
                "task_store", task_id, created_at, now,
            )
            if parent_task_id and re.fullmatch(r"task-[0-9a-f]{32}", parent_task_id):
                parent_entity = self._put_entity(
                    connection, EntityType.TASK, parent_task_id, parent_task_id,
                    GraphState.CURRENT, "task_store", parent_task_id, created_at, now,
                )
                self._put_relationship(
                    connection, task_entity, RelationshipType.TASK_CONTINUES_TASK,
                    parent_entity, GraphState.CURRENT, "task_store", task_id, created_at, updated_at=now,
                )
            for file_id, label in files[:20]:
                if not file_id or len(file_id) > 512 or not label or len(label) > 180:
                    continue
                file_entity = self._put_entity(
                    connection, EntityType.FILE, file_id, label, GraphState.CURRENT,
                    "task_store", task_id, created_at, now,
                )
                self._put_relationship(
                    connection, task_entity, RelationshipType.TASK_USES_FILE, file_entity,
                    GraphState.CURRENT, "task_store", task_id, created_at, updated_at=now,
                )
            for artifact_id, label in artifacts[:20]:
                if not artifact_id or len(artifact_id) > 512 or not label or len(label) > 180:
                    continue
                artifact_entity = self._put_entity(
                    connection, EntityType.ARTIFACT, artifact_id, label, GraphState.CURRENT,
                    "task_store", task_id, created_at, now,
                )
                self._put_relationship(
                    connection, task_entity, RelationshipType.TASK_PRODUCES_ARTIFACT,
                    artifact_entity, GraphState.CURRENT, "task_store", task_id, created_at, updated_at=now,
                )
            current_file_ids = {f"file:{file_id}" for file_id, label in files[:20] if file_id and label}
            current_artifact_ids = {f"artifact:{artifact_id}" for artifact_id, label in artifacts[:20] if artifact_id and label}
            for relation, keep_ids in (
                (RelationshipType.TASK_USES_FILE, current_file_ids),
                (RelationshipType.TASK_PRODUCES_ARTIFACT, current_artifact_ids),
            ):
                existing_edges = connection.execute(
                    "SELECT relationship_id,target_entity_id FROM graph_relationships "
                    "WHERE source_entity_id=? AND relationship_type=? AND state='current'",
                    (task_entity, relation.value),
                ).fetchall()
                stale = [str(edge["relationship_id"]) for edge in existing_edges if str(edge["target_entity_id"]) not in keep_ids]
                if stale:
                    placeholders = ",".join("?" for _ in stale)
                    connection.execute(
                        f"UPDATE graph_relationships SET state='deleted',updated_at=? WHERE relationship_id IN ({placeholders})",
                        (now, *stale),
                    )
            if parent_task_id is None:
                connection.execute(
                    "UPDATE graph_relationships SET state='deleted',updated_at=? WHERE source_entity_id=? "
                    "AND relationship_type=? AND state='current'",
                    (now, task_entity, RelationshipType.TASK_CONTINUES_TASK.value),
                )
            connection.commit()

    def graph_search(self, query: str, *, max_depth: int = 2, max_nodes: int = 32) -> GraphSearchResult:
        """Lexically seed the graph, then traverse current edges within strict bounds."""
        if not 1 <= max_depth <= 3 or not 1 <= max_nodes <= 64:
            raise ValueError("graph traversal bounds are invalid")
        tokens = set(_tokens(query))
        if not tokens:
            return GraphSearchResult((), (), (), ())
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM graph_entities WHERE state='current' ORDER BY updated_at DESC LIMIT 500"
            ).fetchall()
            memory_rows = connection.execute(
                "SELECT m.memory_id,m.content,p.name AS project_name FROM memories m "
                "LEFT JOIN projects p USING(project_id) WHERE m.deleted_at IS NULL AND m.superseded_by IS NULL "
                "ORDER BY m.updated_at DESC LIMIT 500"
            ).fetchall()
            memory_text = {
                str(row["memory_id"]): f"{row['content']} {row['project_name'] or ''}"
                for row in memory_rows
            }
            scored: list[tuple[int, str, GraphEntity]] = []
            for row in rows:
                entity = _graph_entity(row)
                searchable = entity.label
                if entity.entity_type is EntityType.MEMORY:
                    searchable += " " + memory_text.get(entity.external_id, "")
                score = len(tokens & set(_tokens(searchable)))
                if score:
                    scored.append((score, entity.updated_at, entity))
            scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
            matched = tuple(item[2] for item in scored[:min(8, max_nodes)])
            included = {entity.entity_id: entity for entity in matched}
            frontier = set(included)
            relationships: dict[str, GraphRelationship] = {}
            matched_ids = set(included)
            for _ in range(max_depth):
                if not frontier or len(included) >= max_nodes:
                    break
                placeholders = ",".join("?" for _ in frontier)
                edge_rows = connection.execute(
                    "SELECT * FROM graph_relationships WHERE state='current' AND "
                    f"(source_entity_id IN ({placeholders}) OR target_entity_id IN ({placeholders})) LIMIT ?",
                    (*frontier, *frontier, max_nodes * 3),
                ).fetchall()
                next_frontier: set[str] = set()
                for edge_row in edge_rows:
                    edge = _graph_relationship(edge_row)
                    relationships[edge.relationship_id] = edge
                    for neighbor_id in (edge.source_entity_id, edge.target_entity_id):
                        if neighbor_id in included or len(included) >= max_nodes:
                            continue
                        neighbor_row = connection.execute(
                            "SELECT * FROM graph_entities WHERE entity_id=? AND "
                            "(state='current' OR (state='superseded' AND ?=1))",
                            (neighbor_id, int(edge.relationship_type is RelationshipType.MEMORY_SUPERSEDES_MEMORY)),
                        ).fetchone()
                        if neighbor_row is not None:
                            included[neighbor_id] = _graph_entity(neighbor_row)
                            next_frontier.add(neighbor_id)
                frontier = next_frontier
        connected = tuple(entity for key, entity in included.items() if key not in matched_ids)
        source_ids = tuple(dict.fromkeys(
            [entity.source_id for entity in (*matched, *connected)]
            + [edge.source_id for edge in relationships.values()]
        ))
        return GraphSearchResult(matched, connected, tuple(relationships.values()), source_ids)

    def supersession_for(self, memory_id: str) -> GraphRelationship | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM graph_relationships WHERE relationship_type=? AND state='current' "
                "AND source_entity_id=? LIMIT 1",
                (RelationshipType.MEMORY_SUPERSEDES_MEMORY.value, f"memory:{memory_id}"),
            ).fetchone()
        return _graph_relationship(row) if row else None

    def references(self, memory_id: str) -> tuple[tuple[str, str, str], ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT reference_type, reference_id, label FROM memory_references WHERE memory_id=? ORDER BY reference_type,label",
                (memory_id,),
            ).fetchall()
        return tuple((str(row["reference_type"]), str(row["reference_id"]), str(row["label"])) for row in rows)

    def record_memory_for_task(self, task_id: str) -> MemoryRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT m.*, p.name AS project_name FROM memories m LEFT JOIN projects p USING(project_id) "
                "WHERE m.task_id=? AND m.deleted_at IS NULL AND m.superseded_by IS NULL ORDER BY m.created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return _memory_record(row) if row else None

    @staticmethod
    def _prior_update(connection: sqlite3.Connection, update_id: int) -> sqlite3.Row | None:
        if isinstance(update_id, bool) or update_id < 0:
            raise ValueError("Telegram update ID must be a non-negative integer")
        return connection.execute("SELECT result_id,response FROM memory_updates WHERE source_update_id=?", (update_id,)).fetchone()

    @staticmethod
    def _ensure_project(connection: sqlite3.Connection, name: str, update_id: int, now: str) -> str:
        normalized = _normalize(name)
        row = connection.execute("SELECT project_id FROM projects WHERE normalized_name=?", (normalized,)).fetchone()
        if row:
            return str(row["project_id"])
        project_id = "project-" + secrets.token_hex(12)
        connection.execute(
            "INSERT INTO projects VALUES (?, ?, ?, '', 'telegram_owner', ?, ?, ?)",
            (project_id, normalized, name, str(update_id), now, now),
        )
        return project_id


def _tokens(value: str) -> list[str]:
    return [token.casefold() for token in _TOKEN.findall(value) if token.casefold() not in {"the", "and", "that", "with", "about", "from", "this", "uses", "use"}]


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _validate_text(value: str, maximum: int, label: str, *, allow_empty: bool = False) -> str:
    cleaned = " ".join(value.strip().split())
    if not cleaned and not allow_empty:
        raise ValueError(f"{label} cannot be empty")
    if len(cleaned) > maximum:
        raise ValueError(f"{label} exceeds {maximum} characters")
    if any(ord(char) < 32 and char not in "\t" for char in cleaned):
        raise ValueError(f"{label} contains a control character")
    return cleaned


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _memory_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        memory_id=str(row["memory_id"]), project_id=str(row["project_id"]) if row["project_id"] else None,
        project_name=str(row["project_name"]) if row["project_name"] else None,
        kind=str(row["kind"]), content=str(row["content"]), source_type=str(row["source_type"]),
        source_id=str(row["source_id"]), task_id=str(row["task_id"]) if row["task_id"] else None,
        created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
        deleted_at=str(row["deleted_at"]) if row["deleted_at"] else None,
        superseded_by=str(row["superseded_by"]) if row["superseded_by"] else None,
    )


def _project_record(row: sqlite3.Row) -> ProjectRecord:
    return ProjectRecord(
        project_id=str(row["project_id"]), name=str(row["name"]), description=str(row["description"]),
        source_type=str(row["source_type"]), source_id=str(row["source_id"]),
        created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
    )


def _memory_state(row: sqlite3.Row) -> GraphState:
    if row["deleted_at"]:
        return GraphState.DELETED
    if row["superseded_by"]:
        return GraphState.SUPERSEDED
    return GraphState.CURRENT


def _graph_entity(row: sqlite3.Row) -> GraphEntity:
    return GraphEntity(
        entity_id=str(row["entity_id"]), entity_type=EntityType(str(row["entity_type"])),
        external_id=str(row["external_id"]), label=str(row["label"]),
        state=GraphState(str(row["state"])), source_type=str(row["source_type"]),
        source_id=str(row["source_id"]), created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _graph_relationship(row: sqlite3.Row) -> GraphRelationship:
    return GraphRelationship(
        relationship_id=str(row["relationship_id"]), source_entity_id=str(row["source_entity_id"]),
        relationship_type=RelationshipType(str(row["relationship_type"])),
        target_entity_id=str(row["target_entity_id"]), state=GraphState(str(row["state"])),
        source_type=str(row["source_type"]), source_id=str(row["source_id"]),
        created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
    )
