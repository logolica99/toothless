from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nanobot.personal_agent.schedule_parser import parse_schedule_request


def test_one_time_reminder_resolves_tomorrow_in_explicit_zone() -> None:
    proposal = parse_schedule_request(
        "Remind me tomorrow at 9 AM to review my results.",
        timezone="America/Los_Angeles",
        now=datetime(2026, 11, 18, 18, tzinfo=UTC),
    )
    assert proposal is not None
    assert proposal.recurrence == "once"
    assert proposal.profile == "reminder"
    assert proposal.local_start == "2026-11-19T09:00:00"
    assert proposal.resolved_local.startswith("2026-11-19T09:00:00-08:00")
    assert proposal.action == "review my results"


def test_daily_research_is_scoped_to_browser_get_head_and_telegram() -> None:
    proposal = parse_schedule_request(
        "Every morning at 8, research the latest AI agent developments and send me a summary.",
        timezone="America/Los_Angeles",
        now=datetime(2026, 11, 18, 12, tzinfo=UTC),
    )
    assert proposal is not None
    assert proposal.recurrence == "daily"
    assert proposal.profile == "browser"
    assert "GET/HEAD only" in proposal.action
    assert "Telegram chat" in proposal.action
    assert "Do not log in" in proposal.action
    assert proposal.local_start.endswith("T08:00:00")


def test_create_reminder_phrase_uses_inactive_schedule_path() -> None:
    proposal = parse_schedule_request(
        "Create a reminder for me in 2 minutes to play games",
        timezone="America/Los_Angeles",
        now=datetime(2026, 11, 19, 3, 32, tzinfo=UTC),
    )
    assert proposal is not None
    assert proposal.profile == "reminder"
    assert proposal.recurrence == "once"
    assert proposal.action == "play games"


def test_ambiguous_or_unconfigured_time_is_not_guessed() -> None:
    with pytest.raises(ValueError, match="IANA time zone"):
        parse_schedule_request("Remind me tomorrow at 9 to review results", timezone=None)
    with pytest.raises(ValueError, match="AM or PM"):
        parse_schedule_request(
            "Remind me tomorrow at 9 to review results",
            timezone="America/Los_Angeles",
            now=datetime(2026, 11, 18, 12, tzinfo=UTC),
        )


def test_unrecognized_schedule_text_is_not_interpreted() -> None:
    assert parse_schedule_request("Please remind me if the task finishes", timezone="UTC") is None
