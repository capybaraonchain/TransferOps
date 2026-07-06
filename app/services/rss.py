from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import feedparser
import requests

from app.config import Settings
from app.services.integrations import extract_year
from app.services.schemas import CandidatePayload
from app.units import BYTES_PER_GB, BYTES_PER_KB, BYTES_PER_MB, BYTES_PER_TB

SIZE_RE = re.compile(
    r"^\s*(?P<size>[0-9.]+)\s*(?P<unit>KB|MB|GB|TB)\s*;\s*(?P<category>.+?)\s*$", re.I
)
TORRENT_ID_RE = re.compile(r"download\.php(?:/)?(?P<id>\d+)")
Provider_INFO_ID_RE = re.compile(r"(?:/t/|details\.php\?id=)(?P<id>\d+)", re.I)
Provider_PASSKEY_RE = re.compile(r"(?:[?;&]|^)tp=(?P<passkey>[A-Za-z0-9]+)")


def _size_bytes(size_text: str, unit: str) -> int:
    scale = {
        "kb": BYTES_PER_KB,
        "mb": BYTES_PER_MB,
        "gb": BYTES_PER_GB,
        "tb": BYTES_PER_TB,
    }
    return int(float(size_text) * scale[unit.lower()])


def parse_description(description: str) -> tuple[int, str]:
    match = SIZE_RE.match(description.strip())
    if not match:
        return 0, "other"
    return _size_bytes(match.group("size"), match.group("unit")), match.group("category").strip()


def derive_guid(entry: dict[str, Any]) -> str | None:
    url = entry.get("id") or entry.get("guid") or entry.get("link")
    if not url:
        return None
    text = str(url)
    match = TORRENT_ID_RE.search(text)
    if match:
        return f"provider-{match.group('id')}"
    parsed = urlparse(text)
    query_id = parse_qs(parsed.query).get("id")
    if query_id:
        return f"provider-{query_id[0]}"
    return text


def _slug_title(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    slug = re.sub(r"[^A-Za-z0-9]+", ".", value).strip(".")
    return slug or fallback


def extract_ipt_torrent_id(url: str | None) -> str | None:
    if not url:
        return None
    text = str(url)
    match = TORRENT_ID_RE.search(text) or Provider_INFO_ID_RE.search(text)
    if match:
        return match.group("id")
    parsed = urlparse(text)
    query_id = parse_qs(parsed.query).get("id")
    if query_id:
        return query_id[0]
    return None


def extract_ipt_passkey(settings: Settings) -> str | None:
    if not settings.rss_url:
        return None
    match = Provider_PASSKEY_RE.search(settings.rss_url)
    if match:
        return match.group("passkey")
    parsed = urlparse(settings.rss_url)
    passkeys = parse_qs(parsed.query).get("torrent_pass")
    if passkeys:
        return passkeys[0]
    return None


def looks_like_torrent_url(url: str | None) -> bool:
    if not url:
        return False
    text = str(url).lower()
    return "download.php" in text or text.endswith(".torrent") or "torrent_pass=" in text


def canonicalize_download_url(
    settings: Settings,
    tracker: str,
    url: str | None,
    title: str | None,
) -> str | None:
    if not url:
        return None
    if looks_like_torrent_url(url):
        return url
    tracker_text = tracker.lower()
    url_text = str(url).lower()
    if (
        "demo" not in tracker_text
        and "provider" not in tracker_text
        and "provider.example" not in url_text
    ):
        return url
    torrent_id = extract_ipt_torrent_id(url)
    passkey = extract_ipt_passkey(settings)
    if not torrent_id or not passkey:
        return url
    slug = quote(_slug_title(title, torrent_id))
    return f"https://provider.example/download.php/{torrent_id}/{slug}.torrent?torrent_pass={passkey}"


def _bdecode(data: bytes, index: int = 0) -> tuple[Any, int]:
    token = data[index : index + 1]
    if token == b"i":
        end = data.index(b"e", index)
        return int(data[index + 1 : end]), end + 1
    if token == b"l":
        index += 1
        items = []
        while data[index : index + 1] != b"e":
            item, index = _bdecode(data, index)
            items.append(item)
        return items, index + 1
    if token == b"d":
        index += 1
        items: dict[bytes, Any] = {}
        while data[index : index + 1] != b"e":
            key, index = _bdecode(data, index)
            value, index = _bdecode(data, index)
            items[key] = value
        return items, index + 1
    length_end = data.index(b":", index)
    length = int(data[index:length_end])
    start = length_end + 1
    end = start + length
    return data[start:end], end


def _bencode(value: Any) -> bytes:
    if isinstance(value, int):
        return f"i{value}e".encode()
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, str):
        encoded = value.encode()
        return str(len(encoded)).encode() + b":" + encoded
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            parts.append(_bencode(key))
            parts.append(_bencode(value[key]))
        return b"d" + b"".join(parts) + b"e"
    raise TypeError(f"Unsupported bencode type: {type(value)!r}")


def compute_info_hash(download_url: str, session: requests.Session | None = None) -> str | None:
    client = session or requests.Session()
    response = client.get(download_url, timeout=15)
    response.raise_for_status()
    payload, _ = _bdecode(response.content)
    if not isinstance(payload, dict) or b"info" not in payload:
        return None
    return hashlib.sha1(_bencode(payload[b"info"])).hexdigest()


def import_rss(
    settings: Settings,
    session: requests.Session | None = None,
) -> list[CandidatePayload]:
    if not settings.rss_url:
        return []
    feed = feedparser.parse(settings.rss_url)
    candidates: list[CandidatePayload] = []
    for entry in feed.entries[: settings.rss_limit]:
        published = None
        if getattr(entry, "published_parsed", None):
            published = datetime(*entry.published_parsed[:6])
        description = entry.get("description") or ""
        size_bytes = 0
        category = entry.get("category", "other")
        if settings.rss_parse_description and description:
            parsed_size, parsed_category = parse_description(description)
            if parsed_size:
                size_bytes = parsed_size
            if parsed_category != "other":
                category = parsed_category
        guid = derive_guid(entry)
        info_hash = None
        if settings.rss_compute_info_hash and entry.get("link"):
            try:
                info_hash = compute_info_hash(entry["link"], session=session)
            except requests.RequestException:
                info_hash = None
        candidates.append(
            CandidatePayload(
                title=entry.get("title", "unknown"),
                guid=guid,
                tracker=settings.rss_default_tracker,
                category=category,
                release_year=extract_year(entry.get("title")),
                size_bytes=size_bytes,
                freeleech=settings.rss_assume_freeleech,
                published_at=published,
                seeders=None,
                leechers=None,
                download_url=canonicalize_download_url(
                    settings,
                    settings.rss_default_tracker,
                    entry.get("link"),
                    entry.get("title"),
                ),
                info_hash=info_hash,
                source="rss",
                raw_payload=dict(entry),
            )
        )
    return candidates
