from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nanobot.personal_agent.schedule_store import ScheduleStore, _next_daily, _zone, resolve_local


def _create(store: ScheduleStore, *, update: int = 1, recurrence: str = "once", profile: str = "reminder", start: str = "2026-11-19T09:00:00"):
    return store.create_pending(
        source_update_id=update,
        owner_id=665,
        chat_id=665,
        description="Review results" if profile == "reminder" else "Morning research",
        action="Review my results" if profile == "reminder" else "Research AI agent developments using the browser",
        timezone="America/Los_Angeles",
        local_start=start,
        recurrence=recurrence,
        profile=profile,
    )


def test_schedule_approval_is_owner_bound_immutable_and_update_idempotent(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedules.sqlite3")
    record = _create(store)
    duplicate = _create(store)
    assert duplicate.schedule_id == record.schedule_id
    assert duplicate.state == "pending"
    assert store.approve(record.schedule_id, owner_id=7, chat_id=7) is None
    now = datetime(2026, 11, 18, 16, tzinfo=UTC)
    approved = store.approve(record.schedule_id, owner_id=665, chat_id=665, now=now)
    assert approved is not None and approved.state == "active"
    approved_hash = approved.approved_hash
    assert store.is_approved_spec_valid(record.schedule_id)
    assert store.approve(record.schedule_id, owner_id=665, chat_id=665, now=now) is None
    with store._connect() as connection:
        connection.execute("UPDATE schedules SET action='Changed scope' WHERE schedule_id=?", (record.schedule_id,))
    assert not store.is_approved_spec_valid(record.schedule_id)
    assert approved_hash is not None


def test_early_schedule_schema_migrates_without_losing_proposals(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "schedules.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schedules (schedule_id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, "
            "description TEXT NOT NULL, action TEXT NOT NULL, timezone TEXT NOT NULL, local_start TEXT NOT NULL, "
            "recurrence TEXT NOT NULL, next_run_at TEXT, state TEXT NOT NULL, profile TEXT NOT NULL, "
            "authorization_policy TEXT NOT NULL, approved_spec TEXT, approved_hash TEXT, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, state_detail TEXT)"
        )
        connection.execute(
            "INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("schedule-legacy", 42, 42, "Reminder", "Review results", "UTC", "2026-11-19T09:00:00", "once",
             None, "pending", "reminder", "telegram_reminder_delivery_no_inference", None, None, "created", "updated", None),
        )
    migrated = ScheduleStore(database)
    record = migrated.get("schedule-legacy", owner_id=42, chat_id=42)
    assert record is not None and record.description == "Reminder" and record.state == "pending"
    created = _create(migrated, update=10)
    assert created.schedule_id != record.schedule_id


def test_due_occurrence_is_unique_and_late_occurrence_is_not_replayed(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedules.sqlite3")
    record = _create(store)
    now = datetime(2026, 11, 18, 16, tzinfo=UTC)
    approved = store.approve(record.schedule_id, owner_id=665, chat_id=665, now=now)
    assert approved and approved.next_run_at
    due = datetime.fromisoformat(approved.next_run_at)
    claimed = store.claim_due(now=due + timedelta(seconds=20))
    assert len(claimed) == 1 and claimed[0].status == "claimed"
    assert store.claim_due(now=due + timedelta(seconds=20)) == ()
    store.finish_occurrence(claimed[0].occurrence_id, status="succeeded", notification_status="sent", telegram_message_id=123)
    assert store.get(record.schedule_id, owner_id=665, chat_id=665).state == "completed"
    assert store.occurrences(record.schedule_id, owner_id=665, chat_id=665)[0].telegram_message_id == 123

    missed_store = ScheduleStore(tmp_path / "missed.sqlite3")
    missed_record = _create(missed_store, update=2)
    missed_approved = missed_store.approve(missed_record.schedule_id, owner_id=665, chat_id=665, now=now)
    missed_due = datetime.fromisoformat(missed_approved.next_run_at)
    occurrence = missed_store.claim_due(now=missed_due + timedelta(minutes=2))
    assert len(occurrence) == 1 and occurrence[0].status == "missed"
    assert occurrence[0].execution_id is None


def test_daily_schedule_advances_calendar_days_and_dst_is_explicit() -> None:
    zone = _zone("America/Los_Angeles")
    ambiguous, shifted = resolve_local(datetime(2026, 11, 1, 1, 30), zone)
    assert not shifted and ambiguous.fold == 0 and ambiguous.utcoffset() == timedelta(hours=-7)
    nonexistent, shifted = resolve_local(datetime(2026, 3, 8, 2, 30), zone)
    assert shifted and nonexistent.hour == 3 and nonexistent.minute == 0
    previous = datetime(2026, 3, 7, 16, tzinfo=UTC)
    next_run = _next_daily(previous, "America/Los_Angeles", datetime(2026, 3, 7, 8).time())
    assert next_run.astimezone(zone).hour == 8


def test_pause_resume_preserve_approved_scope_and_missed_daily_occurrences(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedules.sqlite3")
    record = _create(store, recurrence="daily", profile="browser")
    now = datetime(2026, 11, 18, 16, tzinfo=UTC)
    approved = store.approve(record.schedule_id, owner_id=665, chat_id=665, now=now)
    assert approved and approved.approved_hash
    paused = store.transition(record.schedule_id, owner_id=665, chat_id=665, target="pause")
    assert paused and paused.state == "paused" and paused.approved_hash == approved.approved_hash
    resumed = store.transition(record.schedule_id, owner_id=665, chat_id=665, target="resume")
    assert resumed and resumed.state == "active" and store.is_approved_spec_valid(record.schedule_id)
    due = datetime.fromisoformat(resumed.next_run_at)
    occurrences = store.claim_due(now=due + timedelta(days=2))
    assert len(occurrences) == 3
    assert [item.status for item in occurrences] == ["missed", "missed", "claimed"]


def test_resume_records_overdue_paused_occurrences_instead_of_replaying(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedules.sqlite3")
    current = datetime.now(UTC).replace(second=0, microsecond=0)
    local_start = (current - timedelta(days=2)).replace(tzinfo=None).isoformat()
    record = store.create_pending(
        source_update_id=3, owner_id=665, chat_id=665, description="Daily research",
        action="Research public AI news using the browser", timezone="UTC", local_start=local_start,
        recurrence="daily", profile="browser",
    )
    approved = store.approve(
        record.schedule_id, owner_id=665, chat_id=665, now=current - timedelta(days=3)
    )
    assert approved is not None
    assert store.transition(record.schedule_id, owner_id=665, chat_id=665, target="pause") is not None
    resumed = store.transition(record.schedule_id, owner_id=665, chat_id=665, target="resume")
    assert resumed is not None and resumed.state == "active" and resumed.next_run_at is not None
    assert datetime.fromisoformat(resumed.next_run_at) > datetime.now(UTC)
    missed = store.occurrences(record.schedule_id, owner_id=665, chat_id=665, limit=10)
    assert missed and all(item.status == "missed" for item in missed)
