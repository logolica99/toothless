"""Owner-approved, host-owned schedules and durable occurrence records."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_SCHEDULE_TEXT = 1200
MAX_MISSED_CATCH_UP = 370
OCCURRENCE_GRACE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ScheduleRecord:
    schedule_id: str
    owner_id: int
    chat_id: int
    description: str
    action: str
    timezone: str
    local_start: str
    recurrence: str
    next_run_at: str | None
    state: str
    profile: str
    authorization_policy: str
    approved_spec: str | None
    approved_hash: str | None
    created_at: str
    updated_at: str
    state_detail: str | None


@dataclass(frozen=True, slots=True)
class OccurrenceRecord:
    occurrence_id: str
    schedule_id: str
    scheduled_for: str
    status: str
    task_id: str | None
    execution_id: str | None
    result: str | None
    error: str | None
    notification_status: str
    telegram_message_id: int | None
    created_at: str
    updated_at: str


class ScheduleStore:
    """Persist schedule proposals, immutable approvals, and idempotent occurrences."""

    def __init__(self, path: Path):
        if not path.is_absolute():
            raise ValueError("schedule database path must be absolute")
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise ValueError("schedule database path must not contain symlinks")
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise ValueError("schedule database directory must be owner-only")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink():
            raise RuntimeError("schedule database must not be a symlink")
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """CREATE TABLE IF NOT EXISTS schedules (
                    schedule_id TEXT PRIMARY KEY,
                    source_update_id INTEGER NOT NULL UNIQUE,
                    owner_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    action TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    local_start TEXT NOT NULL,
                    recurrence TEXT NOT NULL CHECK(recurrence IN ('once','daily')),
                    next_run_at TEXT,
                    state TEXT NOT NULL CHECK(state IN ('pending','active','paused','cancelled','completed')),
                    profile TEXT NOT NULL CHECK(profile IN ('reminder','browser')),
                    authorization_policy TEXT NOT NULL,
                    approved_spec TEXT,
                    approved_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    state_detail TEXT
                );
                CREATE TABLE IF NOT EXISTS schedule_occurrences (
                    occurrence_id TEXT PRIMARY KEY,
                    schedule_id TEXT NOT NULL REFERENCES schedules(schedule_id),
                    scheduled_for TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('claimed','dispatching','running','succeeded','failed','missed','cancelled','delivery_uncertain','unknown')),
                    task_id TEXT,
                    execution_id TEXT,
                    result TEXT,
                    error TEXT,
                    notification_status TEXT NOT NULL CHECK(notification_status IN ('not_applicable','pending','sending','sent','uncertain')),
                    telegram_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(schedule_id, scheduled_for)
                );
                CREATE INDEX IF NOT EXISTS schedule_due_idx ON schedules(state,next_run_at);
                CREATE INDEX IF NOT EXISTS schedule_occurrence_idx ON schedule_occurrences(schedule_id,scheduled_for DESC);
                CREATE INDEX IF NOT EXISTS schedule_execution_idx ON schedule_occurrences(execution_id);"""
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(schedules)")}
            if "source_update_id" not in columns:
                connection.execute("ALTER TABLE schedules ADD COLUMN source_update_id INTEGER")
                connection.execute("UPDATE schedules SET source_update_id=-rowid WHERE source_update_id IS NULL")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS schedule_source_update_idx ON schedules(source_update_id)"
            )
        os.chmod(self.path, 0o600)

    def create_pending(
        self,
        *,
        owner_id: int,
        chat_id: int,
        source_update_id: int,
        description: str,
        action: str,
        timezone: str,
        local_start: str,
        recurrence: str,
        profile: str,
    ) -> ScheduleRecord:
        description = _bounded(description, "schedule description")
        action = _bounded(action, "schedule action")
        _zone(timezone)
        try:
            local = datetime.fromisoformat(local_start)
        except ValueError as exc:
            raise ValueError("invalid local schedule time") from exc
        if local.tzinfo is not None or recurrence not in {"once", "daily"}:
            raise ValueError("schedule time or recurrence is invalid")
        if profile == "browser":
            if recurrence != "daily":
                raise ValueError("browser research schedules must recur daily")
            authorization = "public_https_get_head_browser_only_telegram_delivery"
        elif profile == "reminder":
            if recurrence != "once":
                raise ValueError("reminders must be one-time schedules")
            authorization = "telegram_reminder_delivery_no_inference"
        else:
            raise ValueError("unsupported schedule profile")
        if owner_id <= 0 or chat_id <= 0:
            raise ValueError("invalid schedule owner or chat")
        if isinstance(source_update_id, bool) or source_update_id < 0:
            raise ValueError("invalid Telegram schedule update ID")
        now = _utc_now()
        schedule_id = "schedule-" + secrets.token_hex(12)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO schedules "
                "(schedule_id,source_update_id,owner_id,chat_id,description,action,timezone,local_start,recurrence,next_run_at,state,profile,authorization_policy,approved_spec,approved_hash,created_at,updated_at,state_detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,NULL,'pending',?,?,NULL,NULL,?,?,NULL)",
                (schedule_id, source_update_id, owner_id, chat_id, description, action, timezone, local.isoformat(), recurrence,
                 profile, authorization, now, now),
            )
            row = connection.execute("SELECT schedule_id FROM schedules WHERE source_update_id=?", (source_update_id,)).fetchone()
            connection.commit()
        actual_id = str(row["schedule_id"]) if row else schedule_id
        record = self.get(actual_id, owner_id=owner_id, chat_id=chat_id)
        if record is None:
            raise RuntimeError("created schedule could not be read back")
        return record

    def approve(self, schedule_id: str, *, owner_id: int, chat_id: int, now: datetime | None = None) -> ScheduleRecord | None:
        now_utc = _as_utc(now or datetime.now(UTC))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM schedules WHERE schedule_id=? AND owner_id=? AND chat_id=?",
                (schedule_id, owner_id, chat_id),
            ).fetchone()
            if row is None or row["state"] != "pending":
                connection.rollback()
                return None
            local = datetime.fromisoformat(str(row["local_start"]))
            zone = _zone(str(row["timezone"]))
            scheduled_local, _ = resolve_local(local, zone)
            scheduled_utc = scheduled_local.astimezone(UTC)
            if scheduled_utc <= now_utc:
                connection.rollback()
                raise ValueError("scheduled start must be in the future; create a new proposal")
            spec = {
                "schedule_id": schedule_id,
                "owner_id": owner_id,
                "chat_id": chat_id,
                "description": str(row["description"]),
                "action": str(row["action"]),
                "timezone": str(row["timezone"]),
                "local_start": local.isoformat(),
                "resolved_start": scheduled_local.isoformat(),
                "recurrence": str(row["recurrence"]),
                "profile": str(row["profile"]),
                "authorization_policy": str(row["authorization_policy"]),
                "resource_limits": _resource_limits(str(row["profile"])),
                "dst_policy": {"ambiguous_local_time": "first_fold", "nonexistent_local_time": "shift_forward"},
            }
            serialized = json.dumps(spec, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(serialized.encode()).hexdigest()
            now_text = now_utc.isoformat()
            connection.execute(
                "UPDATE schedules SET state='active',next_run_at=?,approved_spec=?,approved_hash=?,updated_at=? WHERE schedule_id=? AND state='pending'",
                (scheduled_utc.isoformat(), serialized, digest, now_text, schedule_id),
            )
            connection.commit()
        return self.get(schedule_id, owner_id=owner_id, chat_id=chat_id)

    def transition(self, schedule_id: str, *, owner_id: int, chat_id: int, target: str) -> ScheduleRecord | None:
        transitions = {"pause": ("active", "paused"), "resume": ("paused", "active"), "cancel": (("pending", "active", "paused"), "cancelled")}
        if target not in transitions:
            raise ValueError("unsupported schedule transition")
        from_states, to_state = transitions[target]
        if isinstance(from_states, str):
            from_states = (from_states,)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM schedules WHERE schedule_id=? AND owner_id=? AND chat_id=?",
                (schedule_id, owner_id, chat_id),
            ).fetchone()
            if target == "resume" and row is not None and row["state"] == "paused" and row["next_run_at"]:
                now_utc = datetime.now(UTC)
                due = datetime.fromisoformat(str(row["next_run_at"]))
                if due <= now_utc:
                    next_due: datetime | None = due
                    wall_time = datetime.fromisoformat(str(row["local_start"])).time()
                    for _ in range(MAX_MISSED_CATCH_UP):
                        if next_due > now_utc:
                            break
                        occurrence_id = "occ-" + hashlib.sha256(
                            f"{row['schedule_id']}\0{next_due.isoformat()}".encode()
                        ).hexdigest()[:24]
                        timestamp = now_utc.isoformat()
                        connection.execute(
                            "INSERT OR IGNORE INTO schedule_occurrences "
                            "VALUES (?,?,?,'missed',NULL,NULL,NULL,'Schedule was paused when this occurrence became due.',"
                            "'pending',NULL,?,?)",
                            (occurrence_id, row["schedule_id"], next_due.isoformat(), timestamp, timestamp),
                        )
                        if row["recurrence"] == "once":
                            next_due = None
                            break
                        next_due = _next_daily(
                            next_due, str(row["timezone"]), wall_time
                        )
                    if next_due is not None and next_due <= now_utc:
                        next_local = datetime.combine(
                            now_utc.astimezone(_zone(str(row["timezone"]))).date() + timedelta(days=1),
                            wall_time,
                        )
                        next_due = resolve_local(next_local, _zone(str(row["timezone"])))[0].astimezone(UTC)
                    connection.execute(
                        "UPDATE schedules SET next_run_at=?,state_detail=?,updated_at=? WHERE schedule_id=?",
                        (next_due.isoformat() if next_due else None,
                         "overdue occurrences were recorded as missed during resume", now_utc.isoformat(), schedule_id),
                    )
            placeholders = ",".join("?" for _ in from_states)
            changed = connection.execute(
                f"UPDATE schedules SET state=?,updated_at=? WHERE schedule_id=? AND owner_id=? AND chat_id=? AND state IN ({placeholders})",
                (to_state, _utc_now(), schedule_id, owner_id, chat_id, *from_states),
            ).rowcount
            connection.commit()
        return self.get(schedule_id, owner_id=owner_id, chat_id=chat_id) if changed else None

    def claim_due(self, *, now: datetime | None = None, grace_seconds: int = OCCURRENCE_GRACE_SECONDS) -> tuple[OccurrenceRecord, ...]:
        instant = _as_utc(now or datetime.now(UTC))
        results: list[OccurrenceRecord] = []
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM schedules WHERE state='active' AND next_run_at IS NOT NULL AND next_run_at<=? ORDER BY next_run_at LIMIT 32",
                (instant.isoformat(),),
            ).fetchall()
            for row in rows:
                due = datetime.fromisoformat(str(row["next_run_at"]))
                for _ in range(MAX_MISSED_CATCH_UP):
                    if due > instant:
                        break
                    missed = (instant - due).total_seconds() > grace_seconds
                    status = "missed" if missed else "claimed"
                    occurrence_id = "occ-" + hashlib.sha256(f"{row['schedule_id']}\0{due.isoformat()}".encode()).hexdigest()[:24]
                    now_text = instant.isoformat()
                    connection.execute(
                        "INSERT OR IGNORE INTO schedule_occurrences VALUES (?,?,?, ?,NULL,NULL,NULL,NULL,'pending',NULL,?,?)",
                        (occurrence_id, row["schedule_id"], due.isoformat(), status, now_text, now_text),
                    )
                    occurrence_row = connection.execute(
                        "SELECT * FROM schedule_occurrences WHERE occurrence_id=?", (occurrence_id,)
                    ).fetchone()
                    if occurrence_row is not None:
                        results.append(_occurrence(occurrence_row))
                    if row["recurrence"] == "once":
                        connection.execute(
                            "UPDATE schedules SET next_run_at=NULL,updated_at=?,state_detail=? WHERE schedule_id=?",
                            (now_text, "one-time occurrence claimed" if not missed else "one-time occurrence missed", row["schedule_id"]),
                        )
                        break
                    due = _next_daily(due, str(row["timezone"]), datetime.fromisoformat(str(row["local_start"])).time())
                if row["recurrence"] == "daily":
                    connection.execute(
                        "UPDATE schedules SET next_run_at=?,updated_at=? WHERE schedule_id=? AND state='active'",
                        (due.isoformat(), instant.isoformat(), row["schedule_id"]),
                    )
            connection.commit()
        return tuple(results)

    def set_occurrence_intent(self, occurrence_id: str, *, task_id: str, execution_id: str) -> bool:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE schedule_occurrences SET status='dispatching',task_id=?,execution_id=?,updated_at=? "
                "WHERE occurrence_id=? AND status='claimed' AND execution_id IS NULL",
                (task_id, execution_id, _utc_now(), occurrence_id),
            ).rowcount
            connection.commit()
        return changed == 1

    def mark_running(self, execution_id: str) -> None:
        self._update_occurrence(
            "UPDATE schedule_occurrences SET status='running',updated_at=? WHERE execution_id=? AND status='dispatching'",
            (_utc_now(), execution_id),
        )

    def finish_occurrence(
        self, occurrence_id: str, *, status: str, result: str | None = None, error: str | None = None,
        notification_status: str | None = None, telegram_message_id: int | None = None,
    ) -> None:
        allowed = {"succeeded", "failed", "missed", "cancelled", "delivery_uncertain", "unknown"}
        if status not in allowed:
            raise ValueError("invalid terminal occurrence state")
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE schedule_occurrences SET status=?,result=?,error=?,notification_status=COALESCE(?,notification_status),telegram_message_id=COALESCE(?,telegram_message_id),updated_at=? WHERE occurrence_id=?",
                (status, (result or "")[:4000] or None, (error or "")[:1000] or None,
                 notification_status, telegram_message_id, _utc_now(), occurrence_id),
            )
            connection.execute(
                "UPDATE schedules SET state='completed',updated_at=?,state_detail=? WHERE recurrence='once' "
                "AND schedule_id=(SELECT schedule_id FROM schedule_occurrences WHERE occurrence_id=?) "
                "AND state IN ('active','paused') AND ? IN ('succeeded','failed','missed','cancelled','delivery_uncertain','unknown')",
                (_utc_now(), f"one-time occurrence {status}", occurrence_id, status),
            )

    def mark_notification_sending(self, occurrence_id: str) -> bool:
        with closing(self._connect()) as connection:
            changed = connection.execute(
                "UPDATE schedule_occurrences SET notification_status='sending',updated_at=? WHERE occurrence_id=? AND notification_status='pending'",
                (_utc_now(), occurrence_id),
            ).rowcount
        return changed == 1

    def recover_interrupted(self) -> None:
        now = _utc_now()
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE schedule_occurrences SET notification_status='uncertain',status=CASE WHEN execution_id IS NULL THEN 'delivery_uncertain' ELSE status END,error=COALESCE(error,'Controller stopped during scheduled delivery; outcome is uncertain and will not be retried.'),updated_at=? WHERE notification_status='sending'",
                (now,),
            )
            connection.execute(
                "UPDATE schedule_occurrences SET status='unknown',error=COALESCE(error,'Controller restarted while the occurrence was active; it will not be resubmitted.'),updated_at=? WHERE status IN ('claimed','dispatching','running') AND execution_id IS NULL",
                (now,),
            )
            connection.execute(
                "UPDATE schedules SET state='completed',updated_at=?,state_detail='one-time occurrence outcome unknown after restart' "
                "WHERE recurrence='once' AND state='active' AND schedule_id IN "
                "(SELECT schedule_id FROM schedule_occurrences WHERE status='unknown')",
                (now,),
            )

    def unresolved_occurrences(self) -> tuple[OccurrenceRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM schedule_occurrences WHERE status IN ('claimed','dispatching','running') ORDER BY created_at LIMIT 64"
            ).fetchall()
        return tuple(_occurrence(row) for row in rows)

    def pending_notifications(self) -> tuple[OccurrenceRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM schedule_occurrences WHERE notification_status='pending' "
                "AND status IN ('missed','failed','cancelled','unknown','delivery_uncertain') ORDER BY created_at LIMIT 64"
            ).fetchall()
        return tuple(_occurrence(row) for row in rows)

    def get(self, schedule_id: str, *, owner_id: int, chat_id: int) -> ScheduleRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE schedule_id=? AND owner_id=? AND chat_id=?",
                (schedule_id, owner_id, chat_id),
            ).fetchone()
        return _schedule(row) if row is not None else None

    def list_for_owner(self, owner_id: int, chat_id: int, *, limit: int = 20) -> tuple[ScheduleRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM schedules WHERE owner_id=? AND chat_id=? ORDER BY created_at DESC LIMIT ?",
                (owner_id, chat_id, max(1, min(limit, 50))),
            ).fetchall()
        return tuple(_schedule(row) for row in rows)

    def occurrences(self, schedule_id: str, *, owner_id: int, chat_id: int, limit: int = 10) -> tuple[OccurrenceRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT o.* FROM schedule_occurrences o JOIN schedules s USING(schedule_id) WHERE o.schedule_id=? AND s.owner_id=? AND s.chat_id=? ORDER BY o.scheduled_for DESC LIMIT ?",
                (schedule_id, owner_id, chat_id, max(1, min(limit, 20))),
            ).fetchall()
        return tuple(_occurrence(row) for row in rows)

    def schedule_for_occurrence(self, occurrence_id: str) -> ScheduleRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT s.* FROM schedules s JOIN schedule_occurrences o USING(schedule_id) WHERE o.occurrence_id=?",
                (occurrence_id,),
            ).fetchone()
        return _schedule(row) if row is not None else None

    def occurrence_for_execution(self, execution_id: str) -> OccurrenceRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM schedule_occurrences WHERE execution_id=?", (execution_id,)).fetchone()
        return _occurrence(row) if row is not None else None

    def get_occurrence(self, occurrence_id: str) -> OccurrenceRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM schedule_occurrences WHERE occurrence_id=?", (occurrence_id,)).fetchone()
        return _occurrence(row) if row is not None else None

    def is_approved_spec_valid(self, schedule_id: str) -> bool:
        record = self.get_any(schedule_id)
        if record is None or not record.approved_spec or not record.approved_hash:
            return False
        digest = hashlib.sha256(record.approved_spec.encode()).hexdigest()
        try:
            snapshot = json.loads(record.approved_spec)
        except json.JSONDecodeError:
            return False
        try:
            local_start, _ = resolve_local(datetime.fromisoformat(record.local_start), _zone(record.timezone))
        except (ValueError, ZoneInfoNotFoundError):
            return False
        return (
            secrets.compare_digest(digest, record.approved_hash)
            and snapshot.get("schedule_id") == record.schedule_id
            and snapshot.get("owner_id") == record.owner_id
            and snapshot.get("chat_id") == record.chat_id
            and snapshot.get("action") == record.action
            and snapshot.get("profile") == record.profile
            and snapshot.get("timezone") == record.timezone
            and snapshot.get("local_start") == record.local_start
            and snapshot.get("resolved_start") == local_start.isoformat()
            and snapshot.get("recurrence") == record.recurrence
            and snapshot.get("authorization_policy") == record.authorization_policy
            and record.authorization_policy == (
                "public_https_get_head_browser_only_telegram_delivery"
                if record.profile == "browser"
                else "telegram_reminder_delivery_no_inference"
            )
            and record.recurrence == ("daily" if record.profile == "browser" else "once")
            and snapshot.get("resource_limits") == _resource_limits(record.profile)
            and snapshot.get("dst_policy") == {"ambiguous_local_time": "first_fold", "nonexistent_local_time": "shift_forward"}
        )

    def get_any(self, schedule_id: str) -> ScheduleRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM schedules WHERE schedule_id=?", (schedule_id,)).fetchone()
        return _schedule(row) if row is not None else None

    def _update_occurrence(self, statement: str, parameters: tuple[object, ...]) -> None:
        with closing(self._connect()) as connection:
            connection.execute(statement, parameters)


def _bounded(value: str, label: str) -> str:
    cleaned = " ".join(value.strip().split())
    if not cleaned or len(cleaned) > MAX_SCHEDULE_TEXT:
        raise ValueError(f"{label} must contain 1 to {MAX_SCHEDULE_TEXT} characters")
    if any(ord(char) < 32 for char in cleaned):
        raise ValueError(f"{label} contains control characters")
    return cleaned


def _resource_limits(profile: str) -> dict[str, int | bool]:
    return {
        "concurrent_occurrences": 1,
        "missed_occurrence_replay": False,
        "guest_timeout_seconds": 300 if profile == "browser" else 0,
        "max_result_chars": 48_000 if profile == "browser" else 0,
    }


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("time zone must be a valid IANA name") from exc


def resolve_local(local: datetime, zone: ZoneInfo) -> tuple[datetime, bool]:
    if local.tzinfo is not None:
        raise ValueError("expected a local wall-clock time")
    for minute in range(181):
        candidate = local + timedelta(minutes=minute)
        valid: list[datetime] = []
        for fold in (0, 1):
            aware = candidate.replace(tzinfo=zone, fold=fold)
            if aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == candidate:
                if not valid or aware.utcoffset() != valid[-1].utcoffset():
                    valid.append(aware)
        if valid:
            return valid[0], minute > 0
    raise ValueError("could not resolve this local time across a daylight-saving transition")


def _next_daily(previous_utc: datetime, zone_name: str, wall_time: time) -> datetime:
    zone = _zone(zone_name)
    local_date = previous_utc.astimezone(zone).date() + timedelta(days=1)
    local = datetime.combine(local_date, wall_time.replace(tzinfo=None))
    resolved, _ = resolve_local(local, zone)
    return resolved.astimezone(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(UTC)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _schedule(row: sqlite3.Row) -> ScheduleRecord:
    return ScheduleRecord(
        schedule_id=str(row["schedule_id"]), owner_id=int(row["owner_id"]), chat_id=int(row["chat_id"]),
        description=str(row["description"]), action=str(row["action"]), timezone=str(row["timezone"]),
        local_start=str(row["local_start"]), recurrence=str(row["recurrence"]),
        next_run_at=str(row["next_run_at"]) if row["next_run_at"] else None,
        state=str(row["state"]), profile=str(row["profile"]), authorization_policy=str(row["authorization_policy"]),
        approved_spec=str(row["approved_spec"]) if row["approved_spec"] else None,
        approved_hash=str(row["approved_hash"]) if row["approved_hash"] else None,
        created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
        state_detail=str(row["state_detail"]) if row["state_detail"] else None,
    )


def _occurrence(row: sqlite3.Row) -> OccurrenceRecord:
    return OccurrenceRecord(
        occurrence_id=str(row["occurrence_id"]), schedule_id=str(row["schedule_id"]),
        scheduled_for=str(row["scheduled_for"]), status=str(row["status"]),
        task_id=str(row["task_id"]) if row["task_id"] else None,
        execution_id=str(row["execution_id"]) if row["execution_id"] else None,
        result=str(row["result"]) if row["result"] else None,
        error=str(row["error"]) if row["error"] else None,
        notification_status=str(row["notification_status"]),
        telegram_message_id=int(row["telegram_message_id"]) if row["telegram_message_id"] else None,
        created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
    )
