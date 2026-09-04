#!/usr/bin/env python3
"""Sequentially collect VibeLoft users' Twitter/X timelines.

State is stored in repo-local JSON files so the job can pause on X rate limits
and resume from the same user/cursor on the next CronBox tick.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from user import USER_FEATURES  # noqa: E402
from user_tweets import TIMELINE_FEATURES, _parse_tweet, extract_screen_name, load_config  # noqa: E402
from utils.session_manager import get_client, load_cookies_from_file  # noqa: E402

DEFAULT_ACCOUNTS = ROOT / "data" / "vibeloft_twitter_account_stats.json"
FALLBACK_ACCOUNTS = ROOT / "data" / "vibeloft_twitter_accounts.json"
STATUS_FILE = ROOT / "data" / "user_tweets" / "latest_collection_status.json"
STATE_FILE = ROOT / "data" / "user_tweets" / "sequence_state.json"
OUTPUT_DIR = ROOT / "data" / "user_tweets" / "vibeloft"
CHECKPOINT_DIR = ROOT / "data" / "user_tweets" / "checkpoints"
LOCK_FILE = ROOT / "data" / "user_tweets" / ".sequence.lock"
LOCAL_TZ_NAME = "Asia/Shanghai"
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def local_now() -> str:
    return datetime.now(LOCAL_TZ).isoformat()


def epoch_to_utc_iso(epoch: int | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def epoch_to_local_iso(epoch: int | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, LOCAL_TZ).isoformat()


def format_epoch_dual(epoch: int | None) -> str:
    utc_iso = epoch_to_utc_iso(epoch)
    local_iso = epoch_to_local_iso(epoch)
    if not utc_iso:
        return "unknown"
    return f"{utc_iso} / {local_iso} {LOCAL_TZ_NAME}"


def parse_cronbox_args() -> dict[str, Any]:
    raw = os.environ.get("CRONBOX_ARGS")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def slug_handle(handle: str) -> str:
    return handle.strip().lstrip("@").lower()


def parse_twitter_created_at(value: str) -> str | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


def sort_tweets(tweets: list[dict]) -> list[dict]:
    def key(tweet: dict) -> tuple[str, str]:
        iso = parse_twitter_created_at(tweet.get("created_at", "")) or ""
        return iso, str(tweet.get("id") or "")

    return sorted(tweets, key=key, reverse=True)


def summarize_tweets(tweets: list[dict]) -> dict[str, Any]:
    dated = [
        (parse_twitter_created_at(tweet.get("created_at", "")), tweet)
        for tweet in tweets
        if tweet.get("created_at")
    ]
    dated = [(iso, tweet) for iso, tweet in dated if iso]
    if not dated:
        return {
            "tweet_count": len(tweets),
            "latest_tweet_created_at": None,
            "latest_tweet_id": None,
            "oldest_tweet_created_at": None,
            "oldest_tweet_id": None,
        }

    dated.sort(key=lambda item: item[0] or "")
    oldest_iso, oldest_tweet = dated[0]
    latest_iso, latest_tweet = dated[-1]
    return {
        "tweet_count": len(tweets),
        "latest_tweet_created_at": latest_iso,
        "latest_tweet_id": latest_tweet.get("id"),
        "oldest_tweet_created_at": oldest_iso,
        "oldest_tweet_id": oldest_tweet.get("id"),
    }


def load_accounts(path: Path) -> list[dict]:
    if not path.exists() and path == DEFAULT_ACCOUNTS:
        path = FALLBACK_ACCOUNTS
    payload = load_json(path, {})

    rows = payload.get("rows") if isinstance(payload, dict) else None
    if rows:
        accounts = []
        for row in rows:
            handle = row.get("twitter_handle") or row.get("handle")
            if not handle:
                continue
            accounts.append(
                {
                    "handle": extract_screen_name(handle),
                    "twitter_url": row.get("twitter_url") or f"https://x.com/{extract_screen_name(handle)}",
                    "vibeloft_username": row.get("vibeloft_username"),
                    "vibeloft_nickname": row.get("vibeloft_nickname"),
                    "vibeloft_profile_url": row.get("vibeloft_profile_url"),
                }
            )
        return accounts

    raw_accounts = payload.get("accounts") if isinstance(payload, dict) else []
    accounts = []
    for item in raw_accounts or []:
        handle = item.get("handle")
        if not handle:
            continue
        profile = item.get("vibeloft_profile") or {}
        accounts.append(
            {
                "handle": extract_screen_name(handle),
                "twitter_url": item.get("url") or f"https://x.com/{extract_screen_name(handle)}",
                "vibeloft_username": profile.get("username"),
                "vibeloft_nickname": profile.get("nickname"),
                "vibeloft_profile_url": profile.get("url"),
            }
        )
    return accounts


def load_status() -> dict:
    status = load_json(STATUS_FILE, {})
    if not isinstance(status, dict):
        status = {}
    status.setdefault("generated_at", None)
    status.setdefault("users", {})
    return status


def update_user_status(handle: str, patch: dict[str, Any]) -> None:
    status = load_status()
    key = slug_handle(handle)
    current = status["users"].get(key, {})
    current.update(patch)
    current["handle"] = handle
    current["updated_at"] = utc_now()
    status["users"][key] = current
    status["generated_at"] = utc_now()
    atomic_write_json(STATUS_FILE, status)


def output_path_for(handle: str) -> Path:
    return OUTPUT_DIR / f"{slug_handle(handle)}.json"


def checkpoint_path_for(handle: str) -> Path:
    return CHECKPOINT_DIR / f"vibeloft_{slug_handle(handle)}.json"


def load_existing_tweets(handle: str) -> list[dict]:
    path = output_path_for(handle)
    if not path.exists():
        return []
    payload = load_json(path, [])
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        tweets = payload.get("tweets")
        return tweets if isinstance(tweets, list) else []
    return []


def save_user_tweets(handle: str, account: dict, user_id: str, tweets: list[dict]) -> Path:
    path = output_path_for(handle)
    payload = {
        "handle": handle,
        "twitter_url": account.get("twitter_url") or f"https://x.com/{handle}",
        "twitter_rest_id": user_id,
        "vibeloft": {
            "username": account.get("vibeloft_username"),
            "nickname": account.get("vibeloft_nickname"),
            "profile_url": account.get("vibeloft_profile_url"),
        },
        "generated_at": utc_now(),
        "stats": summarize_tweets(tweets),
        "tweets": sort_tweets(tweets),
    }
    atomic_write_json(path, payload)
    return path


def load_checkpoint(handle: str) -> dict | None:
    path = checkpoint_path_for(handle)
    if not path.exists():
        return None
    payload = load_json(path, {})
    return payload if isinstance(payload, dict) else None


def save_checkpoint(handle: str, checkpoint: dict) -> None:
    checkpoint["updated_at"] = utc_now()
    atomic_write_json(checkpoint_path_for(handle), checkpoint)


def clear_checkpoint(handle: str) -> None:
    path = checkpoint_path_for(handle)
    if path.exists():
        path.unlink()


@dataclass
class PageResult:
    tweets: list[dict]
    cursor: str | None
    remaining: int | None
    reset_at_epoch: int | None


class RateLimitPause(RuntimeError):
    def __init__(self, reset_at_epoch: int | None, message: str = "rate_limited") -> None:
        self.reset_at_epoch = reset_at_epoch
        super().__init__(message)

    @property
    def reset_at_iso(self) -> str | None:
        return epoch_to_utc_iso(self.reset_at_epoch)

    @property
    def reset_at_local_iso(self) -> str | None:
        return epoch_to_local_iso(self.reset_at_epoch)


def is_systemic_network_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "certificate_verify_failed",
        "self-signed certificate",
        "ssl:",
        "proxyerror",
        "connection refused",
        "network is unreachable",
        "temporary failure",
    )
    return any(marker in text for marker in markers)


def parse_int_header(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


TRANSIENT_HTTP_EXCEPTIONS = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.PoolTimeout,
)
TRANSIENT_HTTP_STATUS = {500, 502, 503, 504}


def get_with_retries(
    client,
    url: str,
    *,
    params: dict[str, Any],
    label: str,
    retries: int,
    retry_delay: float,
):
    attempts = max(retries, 0) + 1
    for attempt in range(1, attempts + 1):
        try:
            response = client.get(url, params=params)
        except TRANSIENT_HTTP_EXCEPTIONS as exc:
            if attempt >= attempts:
                raise
            wait = retry_delay * attempt
            print(
                f"[retry] {label}: {type(exc).__name__}: {exc}; "
                f"retry {attempt}/{retries} in {wait:.1f}s",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        if response.status_code in TRANSIENT_HTTP_STATUS and attempt < attempts:
            wait = retry_delay * attempt
            print(
                f"[retry] {label}: HTTP {response.status_code}; "
                f"retry {attempt}/{retries} in {wait:.1f}s",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue
        return response

    raise RuntimeError(f"{label}: retry loop exhausted")


def resolve_user_id(
    client,
    handle: str,
    cfg: dict,
    *,
    retries: int = 3,
    retry_delay: float = 5.0,
) -> str:
    query_id = cfg["api"].get("user_by_screenname_query_id", "IGgvgiOx4QZndDHuD3x9TQ")
    url = f"{cfg['api']['base_url']}/graphql/{query_id}/UserByScreenName"
    response = get_with_retries(
        client,
        url,
        params={
            "variables": json.dumps({"screen_name": handle, "withGrokTranslatedBio": True}),
            "features": json.dumps(USER_FEATURES),
            "fieldToggles": json.dumps({"withPayments": False, "withAuxiliaryUserLabels": True}),
        },
        label=f"resolve_user @{handle}",
        retries=retries,
        retry_delay=retry_delay,
    )
    reset_at = parse_int_header(response.headers.get("x-rate-limit-reset"))
    if response.status_code == 429:
        raise RateLimitPause(reset_at, "resolve_user_http_429")
    if response.status_code != 200:
        raise RuntimeError(f"resolve user HTTP {response.status_code}: {response.text[:300]}")

    data = response.json()
    result = data.get("data", {}).get("user", {}).get("result")
    if not result:
        message = "user_not_found_or_inaccessible"
        if data.get("errors"):
            message = data["errors"][0].get("message") or message
        raise RuntimeError(message)
    user_id = result.get("rest_id")
    if not user_id:
        raise RuntimeError("missing twitter rest_id")
    return str(user_id)


def fetch_tweets_page(
    client,
    user_id: str,
    cfg: dict,
    cursor: str | None,
    *,
    retries: int = 3,
    retry_delay: float = 5.0,
) -> PageResult:
    query_id = cfg["api"].get("user_tweets_query_id", "PNd0vlufvrcIwrAnBYKE9g")
    url = f"{cfg['api']['base_url']}/graphql/{query_id}/UserTweets"
    variables: dict[str, Any] = {
        "userId": user_id,
        "count": 20,
        "includePromotedContent": True,
        "withQuickPromoteEligibilityTweetFields": True,
        "withVoice": True,
    }
    if cursor:
        variables["cursor"] = cursor

    response = get_with_retries(
        client,
        url,
        params={
            "variables": json.dumps(variables),
            "features": json.dumps(TIMELINE_FEATURES),
            "fieldToggles": json.dumps({"withArticlePlainText": False}),
        },
        label=f"user_tweets user_id={user_id}",
        retries=retries,
        retry_delay=retry_delay,
    )
    reset_at = parse_int_header(response.headers.get("x-rate-limit-reset"))
    remaining = parse_int_header(response.headers.get("x-rate-limit-remaining"))

    if response.status_code == 429:
        raise RateLimitPause(reset_at, "http_429")
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")

    data = response.json()
    instructions = (
        data.get("data", {})
        .get("user", {})
        .get("result", {})
        .get("timeline", {})
        .get("timeline", {})
        .get("instructions", [])
    )

    tweets: list[dict] = []
    next_cursor = None
    for instr in instructions:
        if instr.get("type") == "TimelinePinEntry":
            result = (
                instr.get("entry", {})
                .get("content", {})
                .get("itemContent", {})
                .get("tweet_results", {})
                .get("result", {})
            )
            tweet = _parse_tweet(result)
            if tweet:
                tweet["pinned"] = True
                tweets.append(tweet)

        for entry in instr.get("entries", []):
            entry_id = entry.get("entryId", "")
            if entry_id.startswith("tweet-"):
                result = (
                    entry.get("content", {})
                    .get("itemContent", {})
                    .get("tweet_results", {})
                    .get("result", {})
                )
                tweet = _parse_tweet(result)
                if tweet:
                    tweets.append(tweet)
            elif entry_id.startswith("cursor-bottom-"):
                next_cursor = entry.get("content", {}).get("value")

    return PageResult(tweets=tweets, cursor=next_cursor, remaining=remaining, reset_at_epoch=reset_at)


def should_pause_for_remaining(page: PageResult, threshold: int) -> bool:
    return page.remaining is not None and page.remaining <= threshold


def collect_one_user(client, cfg: dict, account: dict, args: argparse.Namespace) -> dict[str, Any]:
    handle = account["handle"]
    key = slug_handle(handle)
    existing = load_existing_tweets(handle)
    existing_ids = {str(tweet.get("id")) for tweet in existing if tweet.get("id")}
    merged_by_id = {str(tweet.get("id")): tweet for tweet in existing if tweet.get("id")}

    status = load_status().get("users", {}).get(key, {})
    stop_on_existing = bool(status.get("history_complete")) and not args.force_full

    checkpoint = load_checkpoint(handle)
    cursor = checkpoint.get("cursor") if checkpoint else None
    user_id = checkpoint.get("twitter_rest_id") if checkpoint else None
    pages_done = int(checkpoint.get("pages_done") or 0) if checkpoint else 0

    if not user_id:
        user_id = resolve_user_id(
            client,
            handle,
            cfg,
            retries=args.request_retries,
            retry_delay=args.request_retry_delay,
        )

    update_user_status(
        handle,
        {
            "status": "running",
            "twitter_url": account.get("twitter_url") or f"https://x.com/{handle}",
            "twitter_rest_id": user_id,
            "vibeloft_username": account.get("vibeloft_username"),
            "vibeloft_nickname": account.get("vibeloft_nickname"),
            "vibeloft_profile_url": account.get("vibeloft_profile_url"),
            "started_at": utc_now(),
            "data_file": str(output_path_for(handle).relative_to(ROOT)),
            "stop_on_existing": stop_on_existing,
        },
    )

    new_count = 0
    page_count = 0
    consecutive_empty_pages = 0
    last_cursor = cursor

    while page_count < args.max_pages_per_user:
        page = fetch_tweets_page(
            client,
            user_id,
            cfg,
            last_cursor,
            retries=args.request_retries,
            retry_delay=args.request_retry_delay,
        )
        page_count += 1
        pages_done += 1

        page_new = 0
        for tweet in page.tweets:
            tweet_id = str(tweet.get("id") or "")
            if not tweet_id:
                continue
            if tweet_id not in merged_by_id:
                page_new += 1
                new_count += 1
            merged_by_id[tweet_id] = tweet

        if page_new == 0:
            consecutive_empty_pages += 1
        else:
            consecutive_empty_pages = 0

        all_tweets = sort_tweets(list(merged_by_id.values()))
        save_user_tweets(handle, account, user_id, all_tweets)
        save_checkpoint(
            handle,
            {
                "handle": handle,
                "twitter_rest_id": user_id,
                "cursor": page.cursor,
                "pages_done": pages_done,
                "tweet_count": len(all_tweets),
                "new_count": new_count,
            },
        )

        print(
            f"    page={pages_done} fetched={len(page.tweets)} new={page_new} "
            f"empty_streak={consecutive_empty_pages} total={len(all_tweets)} remaining={page.remaining}"
        )

        if args.max_empty_pages and consecutive_empty_pages >= args.max_empty_pages:
            summary = summarize_tweets(all_tweets)
            clear_checkpoint(handle)
            update_user_status(
                handle,
                {
                    "status": "completed",
                    "completed_at": utc_now(),
                    "completed_at_local": local_now(),
                    "history_complete": True,
                    "pages_collected_total": pages_done,
                    "new_tweets_last_run": new_count,
                    "stop_reason": f"consecutive_empty_pages_{consecutive_empty_pages}",
                    "error": "",
                    **summary,
                },
            )
            return {
                "status": "completed",
                "new_count": new_count,
                "stop_reason": f"consecutive_empty_pages_{consecutive_empty_pages}",
                **summary,
            }

        if should_pause_for_remaining(page, args.pause_remaining_threshold):
            raise RateLimitPause(page.reset_at_epoch, f"remaining_{page.remaining}")

        if not page.cursor:
            summary = summarize_tweets(all_tweets)
            clear_checkpoint(handle)
            update_user_status(
                handle,
                {
                    "status": "completed",
                    "completed_at": utc_now(),
                    "completed_at_local": local_now(),
                    "history_complete": True,
                    "pages_collected_total": pages_done,
                    "new_tweets_last_run": new_count,
                    "error": "",
                    **summary,
                },
            )
            return {"status": "completed", "new_count": new_count, **summary}

        if stop_on_existing and page_new == 0:
            summary = summarize_tweets(all_tweets)
            clear_checkpoint(handle)
            update_user_status(
                handle,
                {
                    "status": "completed",
                    "completed_at": utc_now(),
                    "completed_at_local": local_now(),
                    "history_complete": True,
                    "pages_collected_total": pages_done,
                    "new_tweets_last_run": new_count,
                    "error": "",
                    **summary,
                },
            )
            return {"status": "completed", "new_count": new_count, "stopped_on_existing": True, **summary}

        last_cursor = page.cursor
        if args.delay > 0:
            time.sleep(args.delay)

    all_tweets = sort_tweets(list(merged_by_id.values()))
    summary = summarize_tweets(all_tweets)
    update_user_status(
        handle,
        {
            "status": "partial",
            "completed_at": utc_now(),
            "completed_at_local": local_now(),
            "history_complete": False,
            "pages_collected_total": pages_done,
            "new_tweets_last_run": new_count,
            "error": "max_pages_per_user_reached",
            **summary,
        },
    )
    return {"status": "partial", "new_count": new_count, **summary}


def git_sync_after_user(handle: str) -> None:
    """Commit and push repo-local collection files after one user completes."""
    paths = [
        "data/user_tweets/latest_collection_status.json",
        "data/user_tweets/sequence_state.json",
        "data/user_tweets/checkpoints",
        "data/user_tweets/vibeloft",
    ]
    subprocess.run(["git", "add", *paths], cwd=ROOT, check=True)
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=ROOT,
        check=False,
    )
    if diff.returncode == 0:
        print(f"[git] no collection changes to commit after @{handle}")
        return
    if diff.returncode != 1:
        raise RuntimeError("git diff --cached --quiet failed")

    subprocess.run(
        ["git", "commit", "-m", f"Update VibeLoft Twitter collection for @{handle}"],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(["git", "push"], cwd=ROOT, check=True)
    print(f"[git] committed and pushed collection update for @{handle}")


def load_state() -> dict:
    state = load_json(STATE_FILE, {})
    return state if isinstance(state, dict) else {}


def save_state(patch: dict[str, Any]) -> None:
    state = load_state()
    state.update(patch)
    state["updated_at"] = utc_now()
    atomic_write_json(STATE_FILE, state)


def _lock_holder_alive(pid: int) -> bool:
    """Whether the process that wrote the lock is still running.

    Signal 0 performs the permission and existence checks without delivering
    anything. PermissionError means the pid exists and belongs to someone
    else, which still counts as alive — the safe direction, since the cost of
    being wrong here is a skipped run rather than two of them writing the same
    files at once.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock() -> None:
    """Take the lock, reclaiming it from a process that no longer exists.

    The pid was written into this file from the first version and never read
    back, so a run that died mid-flight left a lock nothing would ever clear:
    every later run exited at this line reporting "sequence job already
    running", which reads like healthy contention rather than a job that has
    been dead for weeks. That is exactly what happened on 2026-08-05 — the run
    started at 10:00 UTC, died around 10:23, and collection stopped for a
    month behind a 60-byte file.

    A lock held by a live pid is still honoured. Only a provably dead holder
    is displaced; anything unreadable is treated as dead, because a lock whose
    owner cannot be identified cannot be waited on either.
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            if attempt == 2:
                raise RuntimeError(f"sequence job already running: {LOCK_FILE}") from exc
            try:
                held = json.loads(LOCK_FILE.read_text() or "{}")
                pid = int(held.get("pid"))
            except (OSError, ValueError, TypeError):
                held, pid = {}, None
            if pid is not None and _lock_holder_alive(pid):
                raise RuntimeError(
                    f"sequence job already running: pid {pid} since "
                    f"{held.get('started_at', 'unknown')} ({LOCK_FILE})"
                ) from exc
            print(
                f"[!] clearing stale lock: pid {pid} from "
                f"{held.get('started_at', 'unknown')} is gone"
            )
            LOCK_FILE.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"pid": os.getpid(), "started_at": utc_now()}) + "\n")
        return


def release_lock() -> None:
    if LOCK_FILE.exists():
        LOCK_FILE.unlink()


def build_parser() -> argparse.ArgumentParser:
    cron_defaults = parse_cronbox_args()
    parser = argparse.ArgumentParser(description="Sequentially collect VibeLoft Twitter/X timelines.")
    parser.add_argument("--accounts", type=Path, default=Path(cron_defaults.get("accounts") or DEFAULT_ACCOUNTS))
    parser.add_argument("--start-index", "--start_index", type=int, default=cron_defaults.get("start_index"))
    parser.add_argument("--max-users-per-run", "--max_users_per_run", type=int, default=int(cron_defaults.get("max_users_per_run") or 0), help="0 means no explicit user limit.")
    parser.add_argument("--max-pages-per-user", "--max_pages_per_user", type=int, default=int(cron_defaults.get("max_pages_per_user") or 10000))
    parser.add_argument("--max-empty-pages", "--max_empty_pages", type=int, default=int(cron_defaults.get("max_empty_pages") or 3), help="Stop a user after N consecutive pages with no new tweets.")
    parser.add_argument("--delay", type=float, default=float(cron_defaults.get("delay") or 2.0))
    parser.add_argument("--request-retries", "--request_retries", type=int, default=int(cron_defaults.get("request_retries") or 3), help="Retries for transient X HTTP/protocol errors.")
    parser.add_argument("--request-retry-delay", "--request_retry_delay", type=float, default=float(cron_defaults.get("request_retry_delay") or 5.0), help="Base seconds for transient request retry backoff.")
    parser.add_argument("--pause-remaining-threshold", "--pause_remaining_threshold", type=int, default=int(cron_defaults.get("pause_remaining_threshold") or 2))
    parser.add_argument("--force-full", "--force_full", action="store_true", default=bool(cron_defaults.get("force_full") or False))
    parser.add_argument("--git-sync", "--git_sync", action="store_true", default=bool(cron_defaults.get("git_sync") or False), help="Commit and push after each fully completed user.")
    parser.add_argument("--insecure-tls", "--insecure_tls", action="store_true", default=bool(cron_defaults.get("insecure_tls") or False), help="Disable TLS certificate verification for local proxy/MITM environments.")
    parser.add_argument("--cookies", default=cron_defaults.get("cookies"))
    parser.add_argument("--auth-token", "--auth_token", default=cron_defaults.get("auth_token"))
    parser.add_argument("--ct0", default=cron_defaults.get("ct0"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    os.chdir(ROOT)
    if args.insecure_tls:
        os.environ["XKIT_INSECURE_TLS"] = "1"

    accounts = load_accounts(args.accounts)
    if not accounts:
        print(f"[x] No VibeLoft Twitter accounts found in {args.accounts}", file=sys.stderr)
        return 1

    state = load_state()
    paused_until = state.get("rate_limit_reset_at")
    if state.get("paused") and paused_until:
        try:
            reset_dt = datetime.fromisoformat(paused_until.replace("Z", "+00:00"))
        except ValueError:
            reset_dt = None
        if reset_dt and reset_dt > datetime.now(UTC):
            print(f"[pause] Rate limit pause is still active until {paused_until}; nothing to do.")
            return 0

    start_index = args.start_index
    if start_index is None:
        start_index = int(state.get("next_index") or 0)
    if start_index >= len(accounts):
        start_index = 0

    auth_token = args.auth_token
    ct0 = args.ct0
    if args.cookies:
        cookie_token, cookie_ct0 = load_cookies_from_file(args.cookies)
        auth_token = auth_token or cookie_token
        ct0 = ct0 or cookie_ct0

    acquire_lock()
    try:
        cfg = load_config()
        client = get_client(auth_token=auth_token, ct0=ct0, verify=not args.insecure_tls)
        completed_this_run = 0
        save_state(
            {
                "paused": False,
                "pause_reason": "",
                "rate_limit_reset_at": None,
                "rate_limit_reset_at_local": None,
                "last_run_started_at": utc_now(),
                "input_file": str(args.accounts),
                "account_count": len(accounts),
                "next_index": start_index,
            }
        )

        with client:
            for index in range(start_index, len(accounts)):
                account = accounts[index]
                handle = account["handle"]
                print(f"[{index + 1}/{len(accounts)}] @{handle}")
                save_state({"next_index": index, "current_handle": handle})
                try:
                    result = collect_one_user(client, cfg, account, args)
                except RateLimitPause as exc:
                    update_user_status(
                        handle,
                        {
                            "status": "paused_rate_limit",
                            "paused_at": utc_now(),
                            "rate_limit_reset_at": exc.reset_at_iso,
                            "rate_limit_reset_at_local": exc.reset_at_local_iso,
                            "error": str(exc),
                        },
                    )
                    save_state(
                        {
                            "paused": True,
                            "pause_reason": str(exc),
                            "rate_limit_reset_at": exc.reset_at_iso,
                            "rate_limit_reset_at_local": exc.reset_at_local_iso,
                            "next_index": index,
                            "current_handle": handle,
                            "last_run_finished_at": utc_now(),
                        }
                    )
                    print(f"[pause] Rate limited at @{handle}; reset_at={format_epoch_dual(exc.reset_at_epoch)}")
                    return 0
                except Exception as exc:
                    if is_systemic_network_error(exc):
                        update_user_status(
                            handle,
                            {
                                "status": "paused_error",
                                "paused_at": utc_now(),
                                "history_complete": False,
                                "error": str(exc),
                            },
                        )
                        save_state(
                            {
                                "paused": True,
                                "pause_reason": str(exc),
                                "rate_limit_reset_at": None,
                                "rate_limit_reset_at_local": None,
                                "next_index": index,
                                "current_handle": handle,
                                "last_run_finished_at": utc_now(),
                            }
                        )
                        print(f"[pause] Systemic network/TLS error at @{handle}: {exc}", file=sys.stderr)
                        return 0

                    update_user_status(
                        handle,
                        {
                            "status": "failed",
                            "failed_at": utc_now(),
                            "history_complete": False,
                            "error": str(exc),
                        },
                    )
                    print(f"[x] @{handle} failed: {exc}", file=sys.stderr)
                    next_index = index + 1
                    save_state(
                        {
                            "next_index": next_index,
                            "current_handle": None,
                            "last_failed_handle": handle,
                            "last_failed_at": utc_now(),
                        }
                    )
                    continue

                print(
                    f"[ok] @{handle} status={result.get('status')} "
                    f"tweets={result.get('tweet_count')} new={result.get('new_count')} "
                    f"latest={result.get('latest_tweet_created_at')}"
                )
                if result.get("status") == "partial":
                    save_state(
                        {
                            "next_index": index,
                            "current_handle": handle,
                            "pause_reason": "partial_user_checkpoint",
                            "last_run_finished_at": utc_now(),
                        }
                    )
                    print(f"[pause] @{handle} is partial; next run will resume same user.")
                    return 0

                completed_this_run += 1
                next_index = index + 1
                stop_after_user = bool(args.max_users_per_run and completed_this_run >= args.max_users_per_run)
                state_patch = {
                    "next_index": next_index,
                    "current_handle": None,
                    "last_completed_handle": handle,
                    "last_completed_at": utc_now(),
                }
                if stop_after_user:
                    state_patch["last_run_finished_at"] = utc_now()
                save_state(state_patch)
                if args.git_sync and result.get("status") == "completed":
                    git_sync_after_user(handle)
                if stop_after_user:
                    print(f"[stop] max-users-per-run={args.max_users_per_run}")
                    return 0

        save_state(
            {
                "paused": False,
                "pause_reason": "",
                "rate_limit_reset_at": None,
                "rate_limit_reset_at_local": None,
                "next_index": 0,
                "current_handle": None,
                "last_full_cycle_completed_at": utc_now(),
                "last_run_finished_at": utc_now(),
            }
        )
        print(f"[done] Completed a full VibeLoft Twitter sequence: {len(accounts)} accounts.")
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
