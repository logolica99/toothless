"""Extract bounded source links from completed research answers."""

from __future__ import annotations

import ipaddress
import re
from typing import cast
from urllib.parse import parse_qsl, urlsplit, urlunsplit

MAX_SOURCES = 12
_URL = re.compile(r"https?://[^\s<>\]\[()]+", re.IGNORECASE)
_MARKDOWN = re.compile(r"[`*_~]")


def validated_sources(answer: str) -> tuple[tuple[str, str], ...]:
    if len(answer) > 48_000:
        return ()
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _URL.finditer(answer):
        raw = match.group(0).rstrip(".,;:!?\"'")
        try:
            parts = urlsplit(raw)
            host = parts.hostname
            if (
                parts.scheme.lower() not in {"http", "https"}
                or not host
                or parts.username
                or parts.password
                or len(raw) > 1000
            ):
                continue
            sensitive_query_keys = {"auth", "authorization", "code", "key", "password", "secret", "session", "signature", "token"}
            if any(any(marker in key.lower() for marker in sensitive_query_keys) for key, _ in parse_qsl(parts.query)):
                continue
            normalized_host = host.encode("idna").decode("ascii").lower().rstrip(".")
            if normalized_host in {"localhost", "localhost.localdomain"} or normalized_host.endswith((".local", ".internal", ".localhost")):
                continue
            try:
                address = ipaddress.ip_address(normalized_host)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                continue
            port = parts.port
            if port not in {None, 80, 443}:
                continue
            scheme = parts.scheme.lower()
            netloc = normalized_host if port is None else f"{normalized_host}:{port}"
            url = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
        except (UnicodeError, ValueError):
            continue
        if url in seen:
            continue
        seen.add(url)
        line_start = answer.rfind("\n", 0, match.start()) + 1
        label = answer[line_start:match.start()].strip()
        label = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", label)
        label = _MARKDOWN.sub("", label).strip(" :-–—[]()")
        title = label[:180] if label else f"{normalized_host} source"
        result.append((title, url))
        if len(result) >= MAX_SOURCES:
            break
    return tuple(result)


def validated_observed_sources(raw: object) -> tuple[tuple[str, str, str, int | None, str], ...]:
    """Validate bounded metadata written by the trusted browser helper, never model prose."""
    if not isinstance(raw, list):
        return ()
    result: list[tuple[str, str, str, int | None, str]] = []
    seen: set[str] = set()
    for item in cast(list[object], raw)[:MAX_SOURCES]:
        if not isinstance(item, dict):
            continue
        source = cast(dict[str, object], item)
        title, url, label, status, evidence = (
            source.get("title"), source.get("url"), source.get("label"), source.get("http_status"),
            source.get("verification_status"),
        )
        if (not isinstance(evidence, str) or evidence not in {"observed", "linked_unvisited"}
                or not isinstance(title, str) or not title.strip() or len(title) > 180
                or not isinstance(url, str) or not url.startswith(("https://", "http://")) or len(url) > 1000
                or not isinstance(label, str) or len(label) > 120
                or (status is not None and (type(status) is not int or not 100 <= status <= 599))):
            continue
        try:
            parts = urlsplit(url)
            host = parts.hostname
            if (parts.scheme.lower() not in {"http", "https"} or not host or parts.username or parts.password
                    or parts.port not in {None, 80, 443} or parts.query or parts.fragment):
                continue
            normalized_host = host.encode("idna").decode("ascii").lower().rstrip(".")
            if normalized_host in {"localhost", "localhost.localdomain"} or normalized_host.endswith((".local", ".internal", ".localhost")):
                continue
            try:
                address = ipaddress.ip_address(normalized_host)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                continue
            safe_url = urlunsplit((parts.scheme.lower(), normalized_host, parts.path or "/", "", ""))
        except (UnicodeError, ValueError):
            continue
        if safe_url in seen:
            continue
        seen.add(safe_url)
        result.append((" ".join(title.split()), safe_url, " ".join(label.split()), status, evidence))
    return tuple(result)
