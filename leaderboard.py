#!/usr/bin/env python3
"""
Overthrow — Nightly Sales Leaderboard

Runs every hour (triggered by GitHub Actions). Only does real work when it's
actually ~10 PM Mountain Time on a Monday-Saturday. Tallies dollar amounts
posted in #daily-sales for the day and posts a ranked leaderboard to
#daily-sales-leaderboard.

Design goals (this replaces a flaky AI-agent-based version):
  - Zero third-party dependencies (stdlib only) so there's nothing extra to
    install or that can break.
  - Every Discord API call is retried automatically before being treated as
    a real failure.
  - If anything at all goes wrong, it posts a visible "something broke"
    alert to the leaderboard channel instead of failing silently.
  - It's safe to run more than once in the same hour/day — it checks what's
    already been posted before posting again.
  - Handles Daylight Saving Time automatically (America/Denver timezone).
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GUILD_ID = "1533899471086161940"
SALES_CHANNEL_ID = "1533899471879143542"
LEADERBOARD_CHANNEL_ID = "1533899471879143544"

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
MOUNTAIN_TZ = ZoneInfo("America/Denver")
API_BASE = "https://discord.com/api/v10"

DOLLAR_PATTERN = re.compile(r"\$[0-9][0-9,]*(?:\.[0-9]{2})?")
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


# ---------------------------------------------------------------------------
# Low-level Discord API helpers (with retries baked in)
# ---------------------------------------------------------------------------

def _discord_request(method, path, params=None, body=None, attempts=3):
    """Call the Discord API, retrying transient failures a few times."""
    url = f"{API_BASE}{path}"
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        if query:
            url = f"{url}?{query}"

    data = json.dumps(body).encode("utf-8") if body is not None else None
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bot {BOT_TOKEN}")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", "ignore")
            last_error = f"HTTP {e.code}: {body_text}"
            # 429 = rate limited, respect Retry-After if present
            if e.code == 429:
                try:
                    retry_after = json.loads(body_text).get("retry_after", 2)
                except Exception:
                    retry_after = 2
                time.sleep(min(float(retry_after), 10))
                continue
        except Exception as e:  # network errors, timeouts, etc.
            last_error = str(e)

        if attempt < attempts:
            time.sleep(2 * attempt)  # 2s, then 4s backoff

    raise RuntimeError(f"Discord API call failed after {attempts} attempts: {method} {path} -> {last_error}")


def list_messages(channel_id, before=None, limit=100):
    return _discord_request(
        "GET",
        f"/channels/{channel_id}/messages",
        params={"limit": limit, "before": before},
    )


def create_message(channel_id, content):
    return _discord_request(
        "POST",
        f"/channels/{channel_id}/messages",
        body={"content": content, "allowed_mentions": {"parse": []}},
    )


def fetch_recent_messages(channel_id, lookback_hours):
    """Fetch all messages newer than `lookback_hours` ago, paginating as needed."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    collected = []
    before = None

    while True:
        batch = list_messages(channel_id, before=before, limit=100)
        if not batch:
            break
        collected.extend(batch)

        oldest = batch[-1]
        oldest_ts = datetime.fromisoformat(oldest["timestamp"])
        if oldest_ts < cutoff or len(batch) < 100:
            break
        before = oldest["id"]

    return collected


# ---------------------------------------------------------------------------
# Tallying
# ---------------------------------------------------------------------------

def mountain_day_bounds(local_date):
    """UTC start/end datetimes for a given Mountain-time calendar date."""
    start_local = datetime(local_date.year, local_date.month, local_date.day, tzinfo=MOUNTAIN_TZ)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def tally_day(messages, day_start_utc, day_end_utc):
    totals = defaultdict(float)
    for m in messages:
        ts = datetime.fromisoformat(m["timestamp"])
        if not (day_start_utc <= ts < day_end_utc):
            continue
        matches = DOLLAR_PATTERN.findall(m.get("content", "") or "")
        if not matches:
            continue
        author = m["author"].get("global_name") or m["author"].get("username") or "Unknown"
        for match in matches:
            totals[author] += float(match.replace("$", "").replace(",", ""))
    return totals


def format_leaderboard(date_label, totals, suffix=""):
    header = f"🏆 DAILY SALES LEADERBOARD — {date_label}{suffix}"
    if not totals:
        return f"{header}\nNo sales posted in #daily-sales today. Get after it tomorrow! 💪"

    ranked = sorted(totals.items(), key=lambda kv: -kv[1])[:10]
    lines = [header, "━" * 32]
    for i, (name, amount) in enumerate(ranked, start=1):
        prefix = MEDALS.get(i, f"{i}.")
        lines.append(f"{prefix} {name} — ${amount:,.0f}")
    lines.append("━" * 32)
    lines.append(f"💰 Team total: ${sum(totals.values()):,.0f}")
    return "\n".join(lines)


def date_label(d):
    return d.strftime("%B ") + str(d.day) + d.strftime(", %Y")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def post_failure_alert(reason=""):
    today_label = date_label(datetime.now(MOUNTAIN_TZ).date())
    msg = f"⚠️ Nightly sales leaderboard automation failed to complete tonight — {today_label} — needs manual review."
    if reason:
        print(f"Posting failure alert. Reason: {reason}", file=sys.stderr)
    create_message(LEADERBOARD_CHANNEL_ID, msg)


def already_posted(recent_msgs, label):
    marker = f"— {label}"
    for m in recent_msgs:
        content = m.get("content", "") or ""
        if content.startswith("🏆 DAILY SALES LEADERBOARD") and marker in content:
            return True
    return False


def run():
    if not BOT_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN environment variable is not set")

    now = datetime.now(MOUNTAIN_TZ)

    # Only run Monday(0)-Saturday(5); never Sunday(6)
    if now.weekday() == 6:
        print("It's Sunday in Mountain Time — nothing to do.")
        return

    # Only do real work in the 10 PM hour, Mountain Time
    if now.hour != 22:
        print(f"Not the 10 PM Mountain hour yet (currently {now.hour}:00 local) — skipping.")
        return

    today = now.date()
    yesterday = today - timedelta(days=1)
    today_lbl = date_label(today)
    yesterday_lbl = date_label(yesterday)

    recent = list_messages(LEADERBOARD_CHANNEL_ID, limit=5)

    # Idempotency: if today's leaderboard (or today's alert) already went out, stop.
    if already_posted(recent, today_lbl):
        print("Today's leaderboard was already posted — skipping.")
        return
    for m in recent:
        if today_lbl in (m.get("content") or "") and "failed to complete" in (m.get("content") or ""):
            print("Today's failure alert was already posted — skipping.")
            return

    # Self-healing: back-fill yesterday if it's missing and yesterday was a run day (Mon-Sat)
    if yesterday.weekday() != 6 and not already_posted(recent, yesterday_lbl):
        sales_msgs = fetch_recent_messages(SALES_CHANNEL_ID, lookback_hours=50)
        y_start, y_end = mountain_day_bounds(yesterday)
        y_totals = tally_day(sales_msgs, y_start, y_end)
        backfill_text = format_leaderboard(
            yesterday_lbl, y_totals, suffix=" (auto-recovered — last night's run didn't post)"
        )
        create_message(LEADERBOARD_CHANNEL_ID, backfill_text)
    else:
        sales_msgs = fetch_recent_messages(SALES_CHANNEL_ID, lookback_hours=26)

    # Today's leaderboard
    t_start, t_end = mountain_day_bounds(today)
    t_totals = tally_day(sales_msgs, t_start, t_end)
    today_text = format_leaderboard(today_lbl, t_totals)
    create_message(LEADERBOARD_CHANNEL_ID, today_text)
    print("Posted today's leaderboard successfully.")


def main():
    try:
        run()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        # Last-resort safety net: try hard to get an alert posted even if
        # something above broke badly. This block deliberately avoids
        # relying on anything that could itself be the cause of failure.
        try:
            post_failure_alert(reason=str(e))
        except Exception as e2:
            print(f"Also failed to post the failure alert: {e2}", file=sys.stderr)
            sys.exit(1)
        sys.exit(1)


if __name__ == "__main__":
    main()
