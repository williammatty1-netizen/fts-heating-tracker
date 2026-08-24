#!/usr/bin/env python3
"""
fts_heating_tracker.py
=======================
 
Automated construction-tracking tool for UK renewable heating projects.
 
Polls the UK Find a Tender Service (FTS) OCDS API for contract notices
that have been updated recently, filters them for a target location
(Greater Manchester boroughs by default) combined with heating /
mechanical-engineering keywords, deduplicates against previously-seen
notices, and pushes a formatted summary of any *new* matches to Telegram
and/or Slack.
 
Designed to be run on a schedule (e.g. daily via GitHub Actions cron) with
no manual intervention. See the bottom of this file / the accompanying
README.md for setup instructions.
 
Author: Senior Python Developer (via Claude)
"""
 
from __future__ import annotations
 
import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse
 
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
 
# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
 
API_BASE = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
API_HOST = "www.find-tender.service.gov.uk"
 
DEFAULT_LOCATION_KEYWORDS = ["Manchester", "Trafford", "Salford", "Stockport"]
DEFAULT_HEATING_KEYWORDS = ["heat pump", "district heat", "HIU", "MVHR", "MEP"]
 
DEFAULT_STATE_FILE = "seen_notices.json"
DEFAULT_LOOKBACK_HOURS = 26  # a little over 24h so a daily cron never leaves a gap
DEFAULT_PAGE_LIMIT = 100
MAX_PAGES_SAFETY_CAP = 200  # hard stop so a pagination bug can never loop forever
 
TELEGRAM_MESSAGE_LIMIT = 3800  # Telegram hard-caps at 4096 chars; leave headroom
SLACK_MESSAGE_LIMIT = 3800
 
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fts_heating_tracker")
 
 
# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
 
@dataclass
class Match:
    ocid: str
    release_id: str
    title: str
    buyer: str
    location: str
    value_amount: Optional[float]
    value_currency: Optional[str]
    deadline: Optional[str]
    stage: str
    updated: str
    url: str
    matched_location_kw: list[str] = field(default_factory=list)
    matched_heating_kw: list[str] = field(default_factory=list)
 
 
# --------------------------------------------------------------------------
# HTTP session with retries
# --------------------------------------------------------------------------
 
def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": "fts-heating-tracker/1.0 (+construction-monitoring-bot)",
            "Accept": "application/json",
        }
    )
    return session
 
 
# --------------------------------------------------------------------------
# Fetching releases from the Find a Tender API
# --------------------------------------------------------------------------
 
def fetch_releases(
    session: requests.Session,
    updated_from: datetime,
    updated_to: datetime,
    limit: int = DEFAULT_PAGE_LIMIT,
    timeout: int = 30,
) -> Iterable[dict[str, Any]]:
    """
    Yield every OCDS release updated within [updated_from, updated_to],
    following the API's cursor-based `links.next` pagination.
 
    The from/to bounds are frozen up front and never touched again while
    paging, per the API's guidance -- only the opaque `cursor` changes.
    """
    params = {
        "updatedFrom": updated_from.strftime("%Y-%m-%dT%H:%M:%S"),
        "updatedTo": updated_to.strftime("%Y-%m-%dT%H:%M:%S"),
        "limit": limit,
    }
    url: Optional[str] = API_BASE
    next_params: Optional[dict[str, Any]] = dict(params)
 
    page = 0
    while url and page < MAX_PAGES_SAFETY_CAP:
        page += 1
        log.debug("Requesting page %d: %s params=%s", page, url, next_params)
        resp = session.get(url, params=next_params, timeout=timeout)
 
        if resp.status_code == 404:
            # No results in range -- FTS returns 404 for an empty window on
            # some deployments instead of an empty releases array.
            log.info("No results for this window (HTTP 404).")
            return
        resp.raise_for_status()
 
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"Non-JSON response from FTS API: {exc}") from exc
 
        releases = payload.get("releases") or []
        log.info("Page %d: %d release(s)", page, len(releases))
        for release in releases:
            yield release
 
        next_url = (payload.get("links") or {}).get("next")
        if not next_url:
            break
 
        # Validate the continuation URL before following it (defence in
        # depth against an unexpected/hostile redirect target).
        parsed = urlparse(next_url)
        if parsed.scheme != "https" or parsed.netloc != API_HOST:
            log.warning("Refusing to follow unexpected pagination host: %s", next_url)
            break
 
        url = next_url
        next_params = None  # the next URL already carries its own query string
 
    if page >= MAX_PAGES_SAFETY_CAP:
        log.warning("Hit MAX_PAGES_SAFETY_CAP (%d) - stopping early.", MAX_PAGES_SAFETY_CAP)
 
 
# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------
 
def _compile_keyword_patterns(keywords: list[str]) -> list[tuple[str, re.Pattern]]:
    """Word-boundary, case-insensitive patterns -- avoids e.g. 'MEP' matching
    inside an unrelated word, while still matching multi-word phrases."""
    patterns = []
    for kw in keywords:
        escaped = re.escape(kw.strip())
        pattern = re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.IGNORECASE)
        patterns.append((kw, pattern))
    return patterns
 
 
def _party_role(party: dict[str, Any], role: str) -> bool:
    return role in (party.get("roles") or [])
 
 
def _extract_buyer(release: dict[str, Any]) -> dict[str, Any]:
    """Resolve the buyer party, whether it's inline in release['buyer'] or
    only referenced by id/name and defined in release['parties']."""
    parties = release.get("parties") or []
    buyer_ref = release.get("buyer") or {}
    buyer_id = buyer_ref.get("id")
 
    for party in parties:
        if buyer_id and party.get("id") == buyer_id:
            return party
        if _party_role(party, "buyer") or _party_role(party, "procuringEntity"):
            return party
 
    return buyer_ref
 
 
def _searchable_text(release: dict[str, Any]) -> str:
    """Concatenate every field that could plausibly contain the project's
    location or heating-technology description into one blob to search."""
    tender = release.get("tender") or {}
    buyer = _extract_buyer(release)
    address = buyer.get("address") or {}
 
    parts: list[str] = [
        tender.get("title") or "",
        tender.get("description") or "",
        buyer.get("name") or "",
        address.get("streetAddress") or "",
        address.get("locality") or "",
        address.get("region") or "",
        address.get("postalCode") or "",
    ]
 
    for item in tender.get("items") or []:
        parts.append(item.get("description") or "")
        for da in item.get("deliveryAddresses") or []:
            parts.append(da.get("locality") or "")
            parts.append(da.get("region") or "")
            parts.append(da.get("postalCode") or "")
            parts.append(da.get("streetAddress") or "")
 
    for lot in tender.get("lots") or []:
        parts.append(lot.get("title") or "")
        parts.append(lot.get("description") or "")
 
    for doc in tender.get("documents") or []:
        parts.append(doc.get("title") or "")
        parts.append(doc.get("description") or "")
 
    return " \n ".join(p for p in parts if p)
 
 
def match_release(
    release: dict[str, Any],
    location_patterns: list[tuple[str, re.Pattern]],
    heating_patterns: list[tuple[str, re.Pattern]],
) -> Optional[tuple[list[str], list[str]]]:
    """Return (matched_location_keywords, matched_heating_keywords) if the
    release matches at least one keyword from EACH group, else None."""
    text = _searchable_text(release)
    if not text.strip():
        return None
 
    loc_hits = [kw for kw, pat in location_patterns if pat.search(text)]
    if not loc_hits:
        return None
 
    heat_hits = [kw for kw, pat in heating_patterns if pat.search(text)]
    if not heat_hits:
        return None
 
    return loc_hits, heat_hits
 
 
def _best_location_string(release: dict[str, Any], matched_kw: list[str]) -> str:
    buyer = _extract_buyer(release)
    address = buyer.get("address") or {}
    bits = [address.get("locality"), address.get("region")]
    bits = [b for b in bits if b]
    if bits:
        return ", ".join(bits)
    return ", ".join(matched_kw) if matched_kw else "Unknown"
 
 
def _notice_url(release: dict[str, Any]) -> str:
    """Prefer a real notice URL from the payload's documents; fall back to
    the always-valid raw OCDS release-package endpoint for the ocid."""
    tender = release.get("tender") or {}
    for doc in tender.get("documents") or []:
        url = doc.get("url")
        if url:
            return url
    ocid = release.get("ocid", "")
    return f"{API_BASE}/{ocid}"
 
 
def build_match(release: dict[str, Any], loc_hits: list[str], heat_hits: list[str]) -> Match:
    tender = release.get("tender") or {}
    buyer = _extract_buyer(release)
    value = tender.get("value") or {}
    tender_period = tender.get("tenderPeriod") or {}
 
    return Match(
        ocid=release.get("ocid", "unknown-ocid"),
        release_id=release.get("id", "unknown-id"),
        title=tender.get("title") or "(no title provided)",
        buyer=buyer.get("name") or "(unknown buyer)",
        location=_best_location_string(release, loc_hits),
        value_amount=value.get("amount"),
        value_currency=value.get("currency"),
        deadline=tender_period.get("endDate"),
        stage=(release.get("tag") or ["unknown"])[0],
        updated=release.get("date") or "",
        url=_notice_url(release),
        matched_location_kw=loc_hits,
        matched_heating_kw=heat_hits,
    )
 
 
# --------------------------------------------------------------------------
# Deduplication / state
# --------------------------------------------------------------------------
 
def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"seen_ocids": {}}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            data.setdefault("seen_ocids", {})
            return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read state file %s (%s) - starting fresh.", path, exc)
        return {"seen_ocids": {}}
 
 
def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic write so a crash mid-write never corrupts the dedupe state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise
 
 
def dedupe_new_matches(matches: list[Match], state: dict[str, Any]) -> list[Match]:
    seen: dict[str, Any] = state["seen_ocids"]
    new_matches = [m for m in matches if m.ocid not in seen]
 
    now_iso = datetime.now(timezone.utc).isoformat()
    for m in new_matches:
        seen[m.ocid] = {
            "title": m.title,
            "first_seen": now_iso,
            "stage": m.stage,
        }
    return new_matches
 
 
# --------------------------------------------------------------------------
# Message formatting
# --------------------------------------------------------------------------
 
def _format_value(m: Match) -> str:
    if m.value_amount is None:
        return "Not disclosed"
    currency = m.value_currency or ""
    try:
        return f"{currency} {m.value_amount:,.0f}".strip()
    except (TypeError, ValueError):
        return f"{currency} {m.value_amount}".strip()
 
 
def _format_deadline(m: Match) -> str:
    if not m.deadline:
        return "Not specified"
    try:
        dt = datetime.fromisoformat(m.deadline.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y %H:%M UTC")
    except ValueError:
        return m.deadline
 
 
def format_match_block(m: Match, plain: bool = False) -> str:
    """`plain=True` renders without Telegram Markdown escaping (used for Slack)."""
    bold = lambda s: s if plain else f"*{s}*"
    link = (lambda text, url: f"{text}: {url}") if plain else (lambda text, url: f"[{text}]({url})")
 
    lines = [
        f"{bold(m.title)}",
        f"Buyer: {m.buyer}",
        f"Location match: {m.location}  (matched: {', '.join(m.matched_location_kw)})",
        f"Heating/MEP match: {', '.join(m.matched_heating_kw)}",
        f"Stage: {m.stage}",
        f"Est. value: {_format_value(m)}",
        f"Deadline: {_format_deadline(m)}",
        f"Last updated: {m.updated}",
        link("View notice", m.url),
    ]
    return "\n".join(lines)
 
 
def chunk_messages(header: str, blocks: list[str], limit: int) -> list[str]:
    """Pack formatted match blocks into as few messages as possible while
    respecting the platform character limit."""
    messages: list[str] = []
    current = header
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages
 
 
# --------------------------------------------------------------------------
# Notification senders
# --------------------------------------------------------------------------
 
def send_telegram(session: requests.Session, bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = session.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    if not resp.ok:
        log.error("Telegram send failed (%s): %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
 
 
def send_slack(session: requests.Session, webhook_url: str, text: str) -> None:
    resp = session.post(webhook_url, json={"text": text}, timeout=15)
    if not resp.ok:
        log.error("Slack send failed (%s): %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
 
 
def notify(
    session: requests.Session,
    matches: list[Match],
    telegram_token: Optional[str],
    telegram_chat_id: Optional[str],
    slack_webhook: Optional[str],
    dry_run: bool = False,
) -> None:
    if not matches:
        log.info("No new matches - nothing to notify.")
        return
 
    header = (
        f"🔥 {len(matches)} new UK heating/MEP tender match(es) found "
        f"(Manchester / Trafford / Salford / Stockport)"
    )
 
    if telegram_token and telegram_chat_id:
        blocks = [format_match_block(m, plain=False) for m in matches]
        for msg in chunk_messages(header, blocks, TELEGRAM_MESSAGE_LIMIT):
            if dry_run:
                log.info("[DRY-RUN][Telegram] Would send:\n%s\n", msg)
            else:
                send_telegram(session, telegram_token, telegram_chat_id, msg)
                time.sleep(1)  # be polite to Telegram's rate limits
        log.info("Telegram notification(s) sent.")
 
    if slack_webhook:
        blocks = [format_match_block(m, plain=True) for m in matches]
        for msg in chunk_messages(header, blocks, SLACK_MESSAGE_LIMIT):
            if dry_run:
                log.info("[DRY-RUN][Slack] Would send:\n%s\n", msg)
            else:
                send_slack(session, slack_webhook, msg)
                time.sleep(1)
        log.info("Slack notification(s) sent.")
 
    if not (telegram_token and telegram_chat_id) and not slack_webhook:
        log.warning(
            "No notification channel configured (set TELEGRAM_BOT_TOKEN + "
            "TELEGRAM_CHAT_ID and/or SLACK_WEBHOOK_URL). Matches found but "
            "nothing sent:\n%s",
            "\n\n".join(format_match_block(m, plain=True) for m in matches),
        )
 
 
# --------------------------------------------------------------------------
# CLI / main
# --------------------------------------------------------------------------
 
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Track UK Find a Tender notices for renewable heating "
        "projects in Greater Manchester and notify Telegram/Slack."
    )
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=float(os.environ.get("FTS_LOOKBACK_HOURS", DEFAULT_LOOKBACK_HOURS)),
        help=f"How far back to query updatedFrom (default: {DEFAULT_LOOKBACK_HOURS}h).",
    )
    parser.add_argument(
        "--state-file",
        default=os.environ.get("FTS_STATE_FILE", DEFAULT_STATE_FILE),
        help=f"Path to the JSON dedupe state file (default: {DEFAULT_STATE_FILE}).",
    )
    parser.add_argument(
        "--location-keywords",
        default=os.environ.get("FTS_LOCATION_KEYWORDS", ",".join(DEFAULT_LOCATION_KEYWORDS)),
        help="Comma-separated location keywords.",
    )
    parser.add_argument(
        "--heating-keywords",
        default=os.environ.get("FTS_HEATING_KEYWORDS", ",".join(DEFAULT_HEATING_KEYWORDS)),
        help="Comma-separated heating/MEP keywords.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("FTS_DRY_RUN", "").lower() in ("1", "true", "yes"),
        help="Find matches and log what would be sent, but don't call Telegram/Slack "
        "and don't update the state file.",
    )
    return parser.parse_args(argv)
 
 
def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
 
    location_keywords = [k.strip() for k in args.location_keywords.split(",") if k.strip()]
    heating_keywords = [k.strip() for k in args.heating_keywords.split(",") if k.strip()]
    location_patterns = _compile_keyword_patterns(location_keywords)
    heating_patterns = _compile_keyword_patterns(heating_keywords)
 
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL")
 
    state_path = Path(args.state_file)
    state = load_state(state_path)
 
    now = datetime.now(timezone.utc)
    updated_from = now - timedelta(hours=args.lookback_hours)
 
    log.info(
        "Querying FTS for updates between %s and %s (location=%s, heating=%s)",
        updated_from.isoformat(),
        now.isoformat(),
        location_keywords,
        heating_keywords,
    )
 
    session = build_session()
 
    try:
        releases = list(fetch_releases(session, updated_from, now))
    except requests.RequestException as exc:
        log.error("Failed to fetch releases from Find a Tender API: %s", exc)
        return 1
 
    log.info("Fetched %d release(s) in window.", len(releases))
 
    matches: list[Match] = []
    for release in releases:
        hit = match_release(release, location_patterns, heating_patterns)
        if hit:
            loc_hits, heat_hits = hit
            matches.append(build_match(release, loc_hits, heat_hits))
 
    log.info("%d release(s) matched location + heating keywords.", len(matches))
 
    new_matches = dedupe_new_matches(matches, state)
    log.info("%d new (not previously notified) match(es).", len(new_matches))
 
    try:
        notify(
            session,
            new_matches,
            telegram_token=telegram_token,
            telegram_chat_id=telegram_chat_id,
            slack_webhook=slack_webhook,
            dry_run=args.dry_run,
        )
    except requests.RequestException as exc:
        log.error("Notification delivery failed: %s", exc)
        # Still persist state below only if not dry-run; a delivery failure
        # after a successful fetch shouldn't be silently swallowed.
        return 2
    finally:
        if not args.dry_run:
            save_state(state_path, state)
            log.info("State file updated: %s (%d total seen)", state_path, len(state["seen_ocids"]))
        else:
            log.info("Dry-run: state file NOT updated.")
 
    return 0
 
 
if __name__ == "__main__":
    sys.exit(main())
