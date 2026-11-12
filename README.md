# Chromium CL watcher — free, no Twilio

Get notified when a specific person's Chromium CLs get a comment or a
Code-Review +1. Polls the public Gerrit API every 5 minutes from GitHub
Actions (free). Stdlib-only Python, no pip installs, no server.

## The honest bit about free SMS

The classic free route — email to `number@vtext.com` / `@txt.att.net` /
`@tmomail.net` — is mostly dead in 2026:

- **AT&T** (`txt.att.net`): shut down June 17, 2025
- **T-Mobile** (`tmomail.net`): stopped December 2024
- **Verizon** (`vtext.com`): still works, but scheduled to shut down
  March 31, 2027, and already drops a lot to spam filtering
- **Google Fi** (`msg.fi.google.com`): the most reliable holdout

So the watcher supports four notifiers, and the reliable free ones are
**Telegram** and **email** (your phone's Gmail push feels like a text):

| Mode | Cost | Reliability | Setup |
|---|---|---|---|
| `telegram` | $0 | High — instant push | BotFather bot + chat id, ~5 min |
| `email` | $0 | High — Gmail push | Gmail app password, ~5 min |
| `sms-gateway` | $0 | Low / dying | Only if Verizon or Google Fi |
| `console` | $0 | — | Testing only |

You can combine them: `NOTIFY_MODE=telegram,email`. The `sms-gateway`
mode is in there if you want to try it, but don't rely on it alone.

## Message format

Telegram messages look like this — the CL title is a tappable link
to the CL:

> 💬 Your CL [Focus] Harden Ephemeral Tab Group Handling in TabStripModel
> has got a comment by Priya Sharma:
> "Can you add a test for the empty-group case?"

> 🔔 Your CL [Focus] Harden Ephemeral Tab Group Handling in TabStripModel
> has got a review (+1) by Alex Rudenko

> 🔔 Your CL [Focus] Harden Ephemeral Tab Group Handling in TabStripModel
> has got a review (-1) by Alex Rudenko

Inline comments arrive as "has got a comment by X — 2 inline
comment(s). Tap the title to read them." (the message itself doesn't
carry the inline text; pass `--inline` to fetch it, at one extra API
call per CL).

## What counts as an event

- **Review**: someone sets Code-Review to +1, -1, +2 or -2 on an open
  CL. Detected from the `Code-Review` label, not from message text, so
  a vote with no words still notifies. Your own votes on your own CLs
  are skipped.
- **Comment**: a human-written top-level comment, or an inline-comment
  batch (shown as "(2 comments)" — the link takes you to the CL).
  Filtered out on purpose: patch-set uploads, CQ / presubmit / LUCI bot
  messages, pure vote messages, "removed reviewer" / "commit message
  was updated" system messages. That filtering was the hard part —
  unfiltered, one author with 25 open CLs produced 513 "events";
  filtered, it's 18 real ones.

## Setup (GitHub Actions, free)

1. Push this folder to a GitHub repo (public = unlimited Actions
   minutes; private = 2,000 free min/month — each run is ~5 seconds,
   so every-5-min is ~580 min/month).
2. Repo > **Settings > Secrets and variables > Actions**:
   - Variables: `GERRIT_AUTHOR` (e.g. `someone@chromium.org`),
     `NOTIFY_MODE` (e.g. `telegram,email`)
   - Secrets: `SMTP_USER` + `SMTP_PASS` (Gmail app password, not your
     login password — Google Account > Security > App passwords),
     `NOTIFY_EMAIL`, and/or `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`,
     and/or `SMS_GATEWAY_ADDRESS`.
3. **Actions > Watch Chromium CLs > Run workflow** to test it.
   The first run baselines — it records everything already there
   without notifying, so you don't get spammed with history.
4. Done. It runs every 5 minutes. State lives in `state.json`,
   committed back to the repo by the workflow so the next run knows
   what it already told you about.

Telegram setup (about 5 minutes):
1. In Telegram, search for **@BotFather**, send `/newbot`, give it a
   name (e.g. `Kunal CL Watcher`) and a username ending in `bot`
   (e.g. `kunal_cl_watcher_bot`). BotFather replies with a token like
   `123456:ABC...` — copy it, and don't paste it in any chat.
2. Find your new bot by its username, tap **Start** (or send it `hi`).
   A bot can't message you until you've messaged it first.
3. Get your chat id: message **@userinfobot** — it replies with your
   numeric id. That number is `TELEGRAM_CHAT_ID`. (Alternative: open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in your browser
   after step 2 and copy the `chat.id` number.)
4. Put the token and chat id in GitHub Secrets (see Setup above),
   never in the repo itself.
5. Test it: Actions > Watch Chromium CLs > Run workflow, tick
   `send_test`, Run. You should get "✅ Your CL watcher is live."
   in Telegram within a minute. Then run it once more without the
   tick to baseline — that run sends nothing, it just records the
   current state.

## Run it locally

```bash
cp .env.example .env   # fill in your values; never commit .env
python3 watcher.py --init        # baseline, no notifications
python3 watcher.py --dry-run     # show what would be sent
python3 watcher.py               # real run (uses NOTIFY_MODE from .env
                                 # — set env vars, or export them first)
python3 watcher.py --author someone@chromium.org --notify telegram
python3 watcher.py --inline      # also fetch inline comment text (slower,
                                 # one extra API call per CL)
```

Env vars are in `.env.example`. The script reads them from the
environment — for local runs, `set -a; source .env; set +a` works, or
export them by hand.

## Files

- `watcher.py` — the whole thing. Poll Gerrit, diff against state,
  notify. Stdlib only.
- `.github/workflows/watch.yml` — cron every 5 min + state commit-back.
- `state.json` — seen message/comment/+1 ids per CL. Starts empty.
- `.env.example` — config template (no secrets in it).
