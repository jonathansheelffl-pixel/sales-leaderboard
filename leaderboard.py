#!/usr/bin/env python3
"""
Overthrow — Sales Leaderboard (one running message per day)

Runs every 10 minutes (triggered by GitHub Actions). Mountain Time drives
all the decisions, handled automatically for Daylight Saving Time:

  - 7:00 AM - 9:59 PM, Monday-Saturday: keeps ONE message per day in
    #daily-sales-leaderboard, editing it in place as sales come in, so
    anyone can check who's on top at any point during the day.
  - 10:00 PM, Monday-Saturday: edits that same message one last time to
    turn it into the final, permanent record for the day (no separate
    message posted) and stops touching it.
  - Sunday, and outside the 7 AM-10 PM window: does nothing.

Design goals (this replaces a flaky AI-agent-based version):
  - Zero third-party dependencies (stdlib only) so there's nothing extra to
    install or that can break.
  - Every Discord API call is retried automatically before being treated as
    a real failure.
  - If anything at all goes wrong, it posts a visible "something broke"
    alert to the leaderboard channel instead of failing silently.
  - No external storage needed — it figures out what's already been posted
    by reading recent messages in the channel, so it's safe to run as often
    as you like without double-posting or duplicating the live message.
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

FINAL_PREFIX = "🏆 DAILY SALES LEADERBOARD"
LIVE_PREFIX = "📊 LIVE LEADERBOARD"
ALERT_MARKER = "failed to complete"

RECENT_SCAN_LIMIT = 20   # how many recent leaderboard-channel messages to check
LIVE_WINDOW_START_HOUR = 7    # 7 AM Mountain
FINAL_POST_HOUR = 22          # 10 PM Mountain


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
            req.add_header("User-Agent", "OverthrowSalesLeaderboardBot (https://github.com, 1.0)")
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


def edit_message(channel_id, message_id, content):
    return _discord_request(
        "PATCH",
        f"/channels/{channel_id}/messages/{message_id}",
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
    """Returns (totals, counts) — dollar totals and number of individual
    sales (policies) per author. A single message can contain more than one
    sale, so each dollar amount found counts as one policy."""
    totals = defaultdict(float)
    counts = defaultdict(int)
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
            counts[author] += 1
    return totals, counts


def format_leaderboard(header, totals, counts, empty_text, footer_extra=None, limit=10):
    """Build the leaderboard message text.

    `limit` caps how many people are listed (final leaderboard shows the
    top 10, as originally specified). Pass limit=None to list everyone who
    has at least one sale — used for the running/live leaderboard so no one
    who sold a policy is left off.
    """
    if not totals:
        lines = [header, empty_text]
        if footer_extra:
            lines.append(footer_extra)
        return "\n".join(lines)

    ranked = sorted(totals.items(), key=lambda kv: -kv[1])
    if limit is not None:
        ranked = ranked[:limit]
    lines = [header, "━" * 32]
    for i, (name, amount) in enumerate(ranked, start=1):
        prefix = MEDALS.get(i, f"{i}.")
        n = counts.get(name, 0)
        sale_word = "sale" if n == 1 else "sales"
        lines.append(f"{prefix} {name} — ${amount:,.0f} ({n} {sale_word})")
    lines.append("━" * 32)
    total_sales = sum(counts.values())
    total_word = "sale" if total_sales == 1 else "sales"
    lines.append(f"💰 Team total: ${sum(totals.values()):,.0f} ({total_sales} {total_word})")
    if footer_extra:
        lines.append(footer_extra)
    return "\n".join(lines)


def date_label(d):
    return d.strftime("%B ") + str(d.day) + d.strftime(", %Y")


def find_message(recent_msgs, prefix, marker):
    """Find the most recent message starting with `prefix` and containing `marker`."""
    for m in recent_msgs:
        content = m.get("content", "") or ""
        if content.startswith(prefix) and marker in content:
            return m
    return None


# ---------------------------------------------------------------------------
# Live (running-total) leaderboard
# ---------------------------------------------------------------------------

def do_live_update(recent, now, today, today_lbl):
    hours_since_midnight = now.hour + now.minute / 60 + 0.5  # small buffer
    sales_msgs = fetch_recent_messages(SALES_CHANNEL_ID, lookback_hours=hours_since_midnight)

    t_start, t_end = mountain_day_bounds(today)
    totals, counts = tally_day(sales_msgs, t_start, t_end)

    header = f"{LIVE_PREFIX} — {today_lbl}"
    body = format_leaderboard(
        header,
        totals,
        counts,
        empty_text="No sales posted yet today — check back soon!",
        limit=None,  # show everyone who has sold, not just the top 10
    )

    existing = find_message(recent, LIVE_PREFIX, today_lbl)
    if existing:
        edit_message(LEADERBOARD_CHANNEL_ID, existing["id"], body)
        print("Live leaderboard edited.")
    else:
        create_message(LEADERBOARD_CHANNEL_ID, body)
        print("Live leaderboard created.")


# ---------------------------------------------------------------------------
# Final (permanent, once-nightly) leaderboard
# ---------------------------------------------------------------------------

def do_backfill_if_needed(recent, yesterday, yesterday_lbl):
    """Self-healing: post yesterday's leaderboard as a new message if it's
    missing and yesterday was a day this automation should have run."""
    if yesterday.weekday() == 6:
        return
    if find_message(recent, FINAL_PREFIX, yesterday_lbl):
        return

    sales_msgs = fetch_recent_messages(SALES_CHANNEL_ID, lookback_hours=50)
    y_start, y_end = mountain_day_bounds(yesterday)
    y_totals, y_counts = tally_day(sales_msgs, y_start, y_end)
    backfill_header = f"{FINAL_PREFIX} — {yesterday_lbl} (auto-recovered — last night's run didn't post)"
    backfill_text = format_leaderboard(
        backfill_header,
        y_totals,
        y_counts,
        empty_text="No sales posted in #daily-sales that day.",
        limit=None,
    )
    create_message(LEADERBOARD_CHANNEL_ID, backfill_text)


def do_finalize(recent, today, today_lbl):
    """Turn today's running message into the final, permanent record.

    If a live message already exists for today, this EDITS that same
    message in place (no new message posted). If somehow no live message
    exists yet (e.g. every earlier run today failed), it posts a fresh one.
    """
    sales_msgs = fetch_recent_messages(SALES_CHANNEL_ID, lookback_hours=26)
    t_start, t_end = mountain_day_bounds(today)
    totals, counts = tally_day(sales_msgs, t_start, t_end)

    header = f"{FINAL_PREFIX} — {today_lbl}"
    body = format_leaderboard(
        header,
        totals,
        counts,
        empty_text="No sales posted in #daily-sales today. Get after it tomorrow! 💪",
        limit=None,  # show everyone who sold, not just the top 10
    )

    existing = find_message(recent, LIVE_PREFIX, today_lbl)
    if existing:
        edit_message(LEADERBOARD_CHANNEL_ID, existing["id"], body)
        print("Finalized today's leaderboard (edited the running message into the final one).")
    else:
        create_message(LEADERBOARD_CHANNEL_ID, body)
        print("Finalized today's leaderboard (no running message existed yet, posted fresh).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def post_failure_alert(reason=""):
    today_label = date_label(datetime.now(MOUNTAIN_TZ).date())
    msg = f"⚠️ Nightly sales leaderboard automation failed to complete tonight — {today_label} — needs manual review."
    if reason:
        print(f"Posting failure alert. Reason: {reason}", file=sys.stderr)
    create_message(LEADERBOARD_CHANNEL_ID, msg)


def run():
    if not BOT_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN environment variable is not set")

    now = datetime.now(MOUNTAIN_TZ)

    # Never run on Sunday
    if now.weekday() == 6:
        print("It's Sunday in Mountain Time — nothing to do.")
        return

    today = now.date()
    yesterday = today - timedelta(days=1)
    today_lbl = date_label(today)
    yesterday_lbl = date_label(yesterday)

    recent = list_messages(LEADERBOARD_CHANNEL_ID, limit=RECENT_SCAN_LIMIT)

    # If today's final leaderboard (or today's failure alert) already went out,
    # the day is done — no more live updates, no re-posting.
    if find_message(recent, FINAL_PREFIX, today_lbl):
        print("Today's final leaderboard was already posted — nothing more to do today.")
        return
    for m in recent:
        content = m.get("content", "") or ""
        if today_lbl in content and ALERT_MARKER in content:
            print("Today's failure alert was already posted — nothing more to do today.")
            return

    if now.hour == FINAL_POST_HOUR:
        do_backfill_if_needed(recent, yesterday, yesterday_lbl)
        do_finalize(recent, today, today_lbl)
        return

    if now.hour < LIVE_WINDOW_START_HOUR:
        print(f"Before the {LIVE_WINDOW_START_HOUR}:00 AM Mountain live window — skipping.")
        return

    do_live_update(recent, now, today, today_lbl)


def main():
    try:
        run()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        # Last-resort safety net: try hard to get an alert posted even if
        # something above broke badly. This block deliberately avoids
        # relying on anything that could itself be the cause of failure.
        # (Only fires for the final/backfill path failing, not for a missed
        # live update — a live-update hiccup will just be corrected on the
        # next run 10 minutes later.)
        now = datetime.now(MOUNTAIN_TZ)
        if now.weekday() != 6 and now.hour == FINAL_POST_HOUR:
            try:
                post_failure_alert(reason=str(e))
            except Exception as e2:
                print(f"Also failed to post the failure alert: {e2}", file=sys.stderr)
                sys.exit(1)
        sys.exit(1)


if __name__ == "__main__":
    main()
