"""Narrow deterministic parser for the first supported schedule phrases."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nanobot.personal_agent.schedule_store import resolve_local

_REMIND_TOMORROW = re.compile(
    r"^remind me tomorrow at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s+to\s+(.+?)[.!]*$",
    re.IGNORECASE,
)
_REMIND_IN = re.compile(r"^remind me in (\d{1,3}) minutes? to (.+?)[.!]*$", re.IGNORECASE)
_CREATE_REMIND_IN = re.compile(
    r"^(?:please )?(?:create|set) a reminder(?: for me)? in (\d{1,3}) minutes? to (.+?)[.!]*$",
    re.IGNORECASE,
)
_RESEARCH_DAILY = re.compile(
    r"^every (morning|day) at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?[, ]+research\s+(.+?)(?:\s+and send me (?:a )?summary)?[.!]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ScheduleProposal:
    description: str
    action: str
    timezone: str
    local_start: str
    resolved_local: str
    recurrence: str
    profile: str
    shifted_for_dst: bool


def parse_schedule_request(
    text: str, *, timezone: str | None, now: datetime | None = None
) -> ScheduleProposal | None:
    """Parse supported owner phrases; return None for unrelated text."""
    raw = text.strip()
    once_match = _REMIND_TOMORROW.fullmatch(raw)
    in_match = _REMIND_IN.fullmatch(raw)
    create_in_match = _CREATE_REMIND_IN.fullmatch(raw)
    research_match = _RESEARCH_DAILY.fullmatch(raw)
    if not any((once_match, in_match, create_in_match, research_match)):
        return None
    if not timezone:
        raise ValueError("Tell me your IANA time zone before I prepare this schedule, for example America/Los_Angeles.")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("The configured time zone is invalid. Set TOOTHLESS_TIME_ZONE to a valid IANA name.") from exc
    now_utc = now or datetime.now(UTC)
    current = now_utc.astimezone(zone)
    if once_match:
        clock = _parse_clock(once_match.group(1), once_match.group(2), once_match.group(3), morning_default=False)
        day = current.date() + timedelta(days=1)
        local = datetime.combine(day, clock)
        action = _clean_action(once_match.group(4))
        profile, recurrence = "reminder", "once"
        description = f"Reminder: {action}"
    elif in_match or create_in_match:
        reminder_match = in_match or create_in_match
        assert reminder_match is not None
        minutes = int(reminder_match.group(1))
        if not 1 <= minutes <= 1440:
            raise ValueError("A one-time reminder must be 1 to 1440 minutes in the future.")
        target = current + timedelta(minutes=minutes)
        if target.second or target.microsecond:
            target += timedelta(minutes=1)
        local = target.replace(tzinfo=None, second=0, microsecond=0)
        action = _clean_action(reminder_match.group(2))
        profile, recurrence = "reminder", "once"
        description = f"Reminder: {action}"
    else:
        assert research_match is not None
        clock = _parse_clock(
            research_match.group(2), research_match.group(3), research_match.group(4),
            morning_default=research_match.group(1).casefold() == "morning",
        )
        local = datetime.combine(current.date(), clock)
        resolved, _ = resolve_local(local, zone)
        if resolved.astimezone(UTC) <= now_utc.astimezone(UTC):
            local += timedelta(days=1)
        topic = _clean_action(research_match.group(5))
        action = (
            f"Research {topic}. Use the isolated browser-only profile to inspect public HTTP(S) pages with GET/HEAD only. "
            "Return a concise source-backed summary with exact URLs. Deliver the result only to the owner's Telegram chat. "
            "Do not log in, submit forms, publish, purchase, or modify accounts."
        )
        profile, recurrence = "browser", "daily"
        description = f"Daily research: {topic}"
    resolved, shifted = resolve_local(local, zone)
    if resolved.astimezone(UTC) <= now_utc.astimezone(UTC):
        raise ValueError("That time is not in the future in the configured time zone. Choose a later time.")
    return ScheduleProposal(
        description=description, action=action, timezone=timezone, local_start=local.isoformat(),
        resolved_local=resolved.isoformat(), recurrence=recurrence, profile=profile,
        shifted_for_dst=shifted,
    )


def _parse_clock(hour_text: str, minute_text: str | None, meridiem: str | None, *, morning_default: bool) -> time:
    hour = int(hour_text)
    minute = int(minute_text or "0")
    if minute > 59:
        raise ValueError("Minute must be between 00 and 59.")
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError("Use an hour from 1 to 12 with AM or PM.")
        hour %= 12
        if meridiem.casefold() == "pm":
            hour += 12
    elif morning_default:
        if not 0 <= hour <= 11:
            raise ValueError("Morning time must be between 0 and 11, or include AM/PM.")
    else:
        raise ValueError("Specify AM or PM so the reminder time is unambiguous.")
    return time(hour, minute)


def _clean_action(value: str) -> str:
    action = " ".join(value.split())
    if not action or len(action) > 900:
        raise ValueError("Schedule action must contain 1 to 900 characters.")
    return action
