#!/usr/bin/env python3
"""
Free Chromium CL watcher — no Twilio, no paid service.

Polls the public Gerrit API for one author's open CLs and notifies you
when there's a new comment or a new Code-Review vote (+1, -1, +2, -2).

Message format (Telegram, title is a tappable link):
  Your CL <title> has got a comment by <name>: "<snippet>"
  Your CL <title> has got a review (+1) by <name>

Free notifiers (pick one or more, comma-separated in NOTIFY_MODE):
  console      - just prints. Useful for testing.
  email        - Gmail SMTP to your own email. Your phone's Gmail push
                 is effectively a text for personal use, and it's reliable.
  telegram     - Telegram bot message. Free, instant, reliable.
                 Create a bot with @BotFather, message it once, then get
                 your chat id from https://api.telegram.org/bot<TOKEN>/getUpdates
  sms-gateway  - Email to your carrier's email-to-SMS gateway, e.g.
                 4155551234@vtext.com
                 WARNING (checked Nov 2026): this route is dying.
                 AT&T txt.att.net shut down Jun 17, 2025.
                 T-Mobile tmomail.net stopped Dec 2024.
                 Verizon vtext.com still works but is scheduled to shut
                 down Mar 31, 2027 and already filters/drops a lot.
                 Google Fi msg.fi.google.com is the most reliable holdout.
                 Only use this if you're on Verizon or Google Fi, and
                 treat Telegram/email as the real notifier.

Hosting (free):
  GitHub Actions cron every 5 min, state committed back to the repo.
  See .github/workflows/watch.yml. Public repo = unlimited minutes,
  private repo = 2,000 free minutes/month. Each run is ~10 seconds,
  so even every 5 min is ~2,900 runs/month x ~0.2 min = ~580 min.

Setup:
  cp .env.example .env   # then fill it in, or set the same vars as
                         # GitHub Secrets for Actions
  python3 watcher.py --init        # first run: baseline, no spam
  python3 watcher.py --dry-run     # test: prints what it *would* send

Config (env vars, or --author / --notify flags):
  GERRIT_AUTHOR         e.g. someone@chromium.org (required)
  GERRIT_HOST           default chromium-review.googlesource.com
  NOTIFY_MODE           default console. e.g. telegram,email
  STATE_FILE            default state.json
  SMTP_HOST / SMTP_PORT default smtp.gmail.com:587
  SMTP_USER / SMTP_PASS Gmail address + app password (not your normal
                        password — Google Account > Security > App passwords)
  NOTIFY_EMAIL          where email notifications go (usually your Gmail)
  SMS_GATEWAY_ADDRESS   e.g. 4155551234@vtext.com (only for sms-gateway mode)
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

Stdlib only — no pip installs.
"""

import argparse
import json
import os
import smtplib
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

GERRIT_HOST = os.environ.get("GERRIT_HOST", "chromium-review.googlesource.com")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")


def gerrit_get(path):
    """GET a Gerrit REST endpoint. Gerrit prefixes JSON with )]}' — strip it."""
    url = f"https://{GERRIT_HOST}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "chromium-cl-watcher/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8")
    if text.startswith(")]}'"):
        text = text[4:].lstrip("\n")
    return json.loads(text)


def query_changes(author):
    q = f"owner:{author} status:open"
    params = urllib.parse.urlencode({
        "q": q,
        "o": ["LABELS", "MESSAGES", "DETAILED_ACCOUNTS"],
        "n": 25,
    }, doseq=True)
    return gerrit_get(f"/changes/?{params}")


def fetch_comments(change_id):
    """Inline (file-level) comments. Returns [] on any error — messages
    are the primary signal, inline comments are a bonus."""
    try:
        data = gerrit_get(f"/changes/{change_id}/comments")
    except Exception as e:
        print(f"  warn: could not fetch comments for {change_id}: {e}", file=sys.stderr)
        return []
    out = []
    for _file, comments in (data or {}).items():
        for c in comments:
            c = dict(c)
            c["_file"] = _file
            out.append(c)
    return out


def change_url(change):
    return f"https://{GERRIT_HOST}/c/{change.get('project')}/+/{change.get('_number')}"


def short(text, limit=120):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


import re

_LABEL_ONLY = re.compile(r"^[A-Za-z][A-Za-z\- ]*[+-]\d$")


def human_comment_body(msg):
    """Return the human-written part of a Gerrit message, or None.

    Gerrit messages are noisy: patch-set uploads, CQ/presubmit votes,
    and LUCI bot runs all appear as messages. A real comment looks like
    'Patch Set 3:\\n\\nLooks good, just nit: ...' — a Patch Set prefix
    followed by free text. A pure vote is just 'Patch Set 3: Code-Review+1'
    with no body; those are caught separately by the +1 detector, so we
    skip them here to avoid double-notifying.
    """
    author = msg.get("author") or {}
    if "SERVICE_USER" in (author.get("tags") or []):
        return None
    email = (author.get("email") or "").lower()
    if email.endswith(".iam.gserviceaccount.com") or "luci" in email:
        return None

    raw = (msg.get("message") or "").strip()
    if raw.startswith("Uploaded patch set"):
        return None
    if re.match(r"^Patch Set \d+ was rebased", raw):
        return None
    # Strip the "Patch Set N:" prefix if present.
    m = re.match(r"^Patch Set \d+:\s*", raw, re.DOTALL)
    body = raw[m.end():].strip() if m else raw
    if not body:
        return None
    # A vote may lead the message ("Code-Review+1\n\nLooks good!").
    # Strip a leading vote for a known Gerrit label, plus any
    # "(Performed by ...)" boilerplate; if nothing human is left,
    # it's not a comment.
    _labels = (r"Code-Review|Commit-Queue|Presubmit-Verified|Auto-Submit|"
               r"Verified|Code-Coverage|Lint|Owners-Override|Mega-CQ|Feels|"
               r"SLSA-Policy-Verified|Bot-Commit|Pure-Revert|Benign-Change|"
               r"Pure-Cherry-Pick|Recitation-Check")
    body = re.sub(r"^\([Pp]erformed by .*$", "", body, flags=re.MULTILINE).strip()
    # Strip leading votes, repeatedly — a message can carry several,
    # e.g. "Auto-Submit+1 Commit-Queue+1".
    while True:
        vm = re.match(rf"^[+-]?(?:{_labels})(?:[+-]\d)?\b\s*", body)
        if not vm:
            break
        body = body[vm.end():].strip()
        body = re.sub(r"^\([Pp]erformed by .*$", "", body,
                      flags=re.MULTILINE).strip()
    if not body:
        return None  # pure vote, e.g. "Commit-Queue+1" / "-Commit-Queue"
    if body.startswith(("Dry run:", "This CL has", "Bot data:",
                        "Published edit", "Copied Votes:", "Outdated Votes:",
                        "This change is ready for review.",
                        "Patch Set", "Removed reviewer", "Added reviewer",
                        "Commit message was updated",
                        "Hashtag added:", "Hashtag removed:",
                        "Set Work In Progress", "Removed Work In Progress",
                        "Set Ready For Review")):
        return None
    if "who was added as reviewer owns" in body:
        return None
    return body


def collect_events(author, state, include_inline=False):
    """Return (events, new_state). Events are dicts with kind/text/link.

    The tracked author is the CL owner, so their own messages, inline
    comments and votes are skipped — the point is to hear from reviewers.
    """
    owner_email = author.lower()
    events = []
    changes = query_changes(author)
    print(f"found {len(changes)} open CL(s) for {author}")

    for ch in changes:
        cid = str(ch.get("id") or ch["_number"])
        num = ch.get("_number")
        subject = ch.get("subject", "")
        seen = state.setdefault(cid, {
            "number": num,
            "subject": subject,
            "message_ids": [],
            "comment_ids": [],
            "plus_ones": [],  # legacy: account ids who gave +1
            "review_votes": {},  # account id -> Code-Review value
        })
        seen["subject"] = subject
        # Migrate legacy +1s so they aren't re-notified.
        for acct in seen.get("plus_ones", []):
            seen.setdefault("review_votes", {}).setdefault(str(acct), 1)
        url = change_url(ch)

        # --- top-level messages (human comments live here) ---
        for m in ch.get("messages", []) or []:
            mid = m.get("id")
            if not mid or mid in seen["message_ids"]:
                continue
            seen["message_ids"].append(mid)
            if ((m.get("author") or {}).get("email") or "").lower() == owner_email:
                continue  # your own message on your own CL
            body = human_comment_body(m)
            if not body:
                continue  # upload / bot / pure vote — not a comment
            author_name = (m.get("author") or {}).get("name", "someone")
            events.append({
                "kind": "comment",
                "number": num,
                "subject": subject,
                "author": author_name,
                "body": short(body, 200),
                "link": url,
            })

        # --- inline comments (opt-in: published inline comments already
        # appear as a message above, so this is just extra detail and
        # costs one extra API call per CL) ---
        for c in (fetch_comments(cid) if include_inline else []):
            comment_id = c.get("id")
            if not comment_id or comment_id in seen["comment_ids"]:
                continue
            seen["comment_ids"].append(comment_id)
            if ((c.get("author") or {}).get("email") or "").lower() == owner_email:
                continue  # your own inline comment on your own CL
            author_name = (c.get("author") or {}).get("name", "someone")
            events.append({
                "kind": "comment",
                "number": num,
                "subject": subject,
                "author": author_name,
                "body": f"on {c.get('_file')}: {short(c.get('message'), 140)}",
                "link": url,
            })

        # --- Code-Review votes (+1, -1, +2, -2) ---
        labels = (ch.get("labels") or {}).get("Code-Review") or {}
        for vote in labels.get("all", []) or []:
            value = vote.get("value")
            if value not in (1, -1, 2, -2):
                continue
            acct = str(vote.get("_account_id"))
            if seen.get("review_votes", {}).get(acct) == value:
                continue
            seen.setdefault("review_votes", {})[acct] = value
            if (vote.get("email") or "").lower() == owner_email:
                continue  # your own vote on your own CL
            name = vote.get("name", "someone")
            events.append({
                "kind": "review",
                "number": num,
                "subject": subject,
                "author": name,
                "vote": value,
                "link": url,
            })

    return events, state


def send_email(subject, body, to_addr):
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(os.environ.get("SMTP_HOST", "smtp.gmail.com"),
                      int(os.environ.get("SMTP_PORT", "587"))) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


import html as _html


def format_event(event, mode):
    """Render an event in the user's format.

    Telegram (HTML, title is a tappable link):
      Your CL <title> has got a comment by <name>: "<snippet>"
      Your CL <title> has got a review (+1) by <name>
    Plain text (console / email / SMS) is the same wording, with the
    title in quotes and the link after it.
    """
    title = event.get("subject") or f"CL {event.get('number')}"
    author = event.get("author", "someone")
    url = event["link"]
    if event["kind"] == "review":
        vote = event["vote"]
        sign = f"+{vote}" if vote > 0 else str(vote)
        if mode == "telegram":
            return (f"🔔 Your CL <a href=\"{_html.escape(url)}\">"
                    f"{_html.escape(title)}</a> has got a review "
                    f"({sign}) by {_html.escape(author)}")
        return (f"Your CL \"{title}\" ({url}) has got a review "
                f"({sign}) by {author}")
    # comment
    body = event.get("body", "")
    m = re.match(r"^\((\d+) comments?\)$", body.strip())
    if m:
        n = m.group(1)
        if mode == "telegram":
            return (f"💬 Your CL <a href=\"{_html.escape(url)}\">"
                    f"{_html.escape(title)}</a> has got a comment by "
                    f"{_html.escape(author)} — {n} inline comment(s). "
                    f"Tap the title to read them.")
        return (f"Your CL \"{title}\" ({url}) has got a comment by "
                f"{author} — {n} inline comment(s).")
    if mode == "telegram":
        return (f"💬 Your CL <a href=\"{_html.escape(url)}\">"
                f"{_html.escape(title)}</a> has got a comment by "
                f"{_html.escape(author)}:\n“{_html.escape(body)}”")
    return (f"Your CL \"{title}\" ({url}) has got a comment by "
            f"{author}: \"{body}\"")


def event_summary(event):
    if event["kind"] == "review":
        return f"review ({event['vote']:+d}) by {event['author']} on CL {event['number']}"
    return f"comment by {event['author']} on CL {event['number']}"


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    data = json.dumps({"chat_id": chat_id, "text": text,
                       "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def notify(event, modes, dry_run=False):
    if dry_run:
        for mode in modes:
            print(f"[dry-run:{mode}] {format_event(event, mode)}")
        return
    if "console" in modes:
        print(format_event(event, "console"))
    if "email" in modes:
        subject = (f"Your CL '{event.get('subject')}' has got a "
                   f"{'review' if event['kind'] == 'review' else 'comment'}")
        send_email(subject, format_event(event, "email"),
                   os.environ["NOTIFY_EMAIL"])
    if "sms-gateway" in modes:
        # Keep it short — SMS segments are 160 chars.
        send_email("", format_event(event, "email")[:150],
                   os.environ["SMS_GATEWAY_ADDRESS"])
    if "telegram" in modes:
        send_telegram(format_event(event, "telegram"))
    print(f"sent ({event_summary(event)})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--author", default=os.environ.get("GERRIT_AUTHOR"))
    ap.add_argument("--notify", default=os.environ.get("NOTIFY_MODE", "console"))
    ap.add_argument("--state", default=STATE_FILE)
    ap.add_argument("--init", action="store_true",
                    help="baseline current state without sending anything")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be sent, send nothing")
    ap.add_argument("--inline", action="store_true",
                    help="also fetch file-level inline comments (slower)")
    ap.add_argument("--send-test", action="store_true",
                    help="send a test notification and exit (does not "
                         "touch state.json)")
    args = ap.parse_args()

    if not args.author:
        ap.error("set GERRIT_AUTHOR or pass --author someone@chromium.org")

    modes = [m.strip() for m in args.notify.split(",") if m.strip()]

    if args.send_test:
        test = ("✅ Your CL watcher is live.\nYou'll get a message here "
                "when one of your CLs has got a comment or a review.")
        if args.dry_run:
            print(f"[dry-run] test message:\n{test}")
            return
        if "console" in modes:
            print(test)
        if "telegram" in modes:
            send_telegram(test)
        if "email" in modes:
            send_email("CL watcher test", test, os.environ["NOTIFY_EMAIL"])
        print("test message sent.")
        return
    state = {}
    if os.path.exists(args.state):
        with open(args.state) as f:
            state = json.load(f)

    is_first_run = not state
    events, state = collect_events(args.author, state,
                                   include_inline=args.inline)

    # A first run baselines silently so you don't get spammed with history.
    # --dry-run is the exception: it should show what *would* be sent.
    if args.init or (is_first_run and not args.dry_run):
        print(f"baselined {len(state)} CL(s), {len(events)} past event(s) "
              f"recorded without notifying. Future runs will only alert on new ones.")
        events = []

    for ev in events:
        try:
            notify(ev, modes, dry_run=args.dry_run)
        except Exception as e:
            print(f"FAILED to notify {event_summary(ev)}: {e}", file=sys.stderr)

    if not args.dry_run:
        with open(args.state, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    print(f"done: {len(events)} new event(s).")


if __name__ == "__main__":
    main()
