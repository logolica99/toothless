from __future__ import annotations

from nanobot.personal_agent.research_sources import validated_observed_sources, validated_sources


def test_source_metadata_is_bounded_and_rejects_local_or_credential_urls() -> None:
    result = validated_sources(
        "Official docs: https://www.python.org/downloads/\n"
        "Bad: http://127.0.0.1/private\n"
        "Bad: https://user:pass@example.com/secret\n"
        "Bad: https://example.com/path?access_token=secret\n"
        "Bad: http://example.com:8080/\n"
        "Bad: https://example.com/" + ("x" * 1000) + "\n"
        "Duplicate: https://www.python.org/downloads/"
    )
    assert result == (("Official docs", "https://www.python.org/downloads/"),)


def test_observed_source_metadata_requires_trusted_observation_and_keeps_only_metadata() -> None:
    result = validated_observed_sources([
        {"title": "AI · Blogs", "url": "https://blogs.example.com/tags/ai", "label": "AI", "http_status": 200,
         "verification_status": "observed", "text": "must not be read"},
        {"title": "Guess", "url": "https://blogs.example.com/tags/guessed", "label": "", "http_status": None,
         "verification_status": "inferred"},
        {"title": "Private", "url": "http://127.0.0.1/private", "label": "", "http_status": 200,
         "verification_status": "observed"},
        {"title": "Query", "url": "https://example.com/?token=secret", "label": "", "http_status": 200,
         "verification_status": "observed"},
    ])
    assert result == (("AI · Blogs", "https://blogs.example.com/tags/ai", "AI", 200, "observed"),)
