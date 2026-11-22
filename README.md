# Gmail Job-Search Assistant

Reads your Gmail, sorts every job-search email (offer, interview, test, rejection,
application received, recruiter outreach, job alert, other), keeps a tracker of each
application and its latest status, lists what needs your action, and drafts replies.

**It never sends mail.** Replies are saved to Gmail's Drafts folder; you read, edit
and send them yourself. The Gmail client in `app/gmail_client.py` has no send method.

## What it looks like

    > python -m app.cli sync
    12 new emails to sort
    [1/12] INTERVIEW *ACTION* Interview invitation - Data Analyst Intern
    [2/12] RECEIVED  Thank you for Applying
    [3/12] alert     25 new jobs matching Software Developer
    ...
    APPLICATIONS
      INTERVIEW Kestrel Analytics - Data Analyst Intern  (updated 2026-11-21)
      RECEIVED  Helix Health - Software Intern  (updated 2026-11-12)
    NEEDS YOUR ACTION
      [18f2c...] INTERVIEW by 2026-11-25: Pick an interview slot for the Data Analyst Intern role.

    > python -m app.cli draft 18f2c... --note "prefer a morning slot"

## How it works

| Part | File | What it does |
|---|---|---|
| Gmail access | `app/gmail_client.py` | OAuth sign-in, list and read messages, parse MIME bodies (plain text or HTML), create threaded reply drafts |
| Sorting | `app/classify.py` | Known bulk senders are labelled by rule with no model call; everything else goes to an LLM that returns JSON, which is validated before use |
| Tracker | `app/tracker.py` | SQLite. One row per email, one per application. Status only moves forward, so a late "we received your application" never undoes an interview |
| Replies | `app/drafts.py` | LLM drafts from your profile facts only; anything it doesn't know becomes a `[placeholder]` you must fill |
| Models | `app/gateway.py` | One place that calls models; tries NVIDIA's hosted endpoint, then a local Ollama model, and reports every attempt |
| Monitoring | `app/monitor.py` | Logs every model call (latency, tokens, provider, errors) to `logs/runs.jsonl`; optionally sends OpenTelemetry traces to Langfuse |

Safety choices:
- **Drafts only.** Sending is left to you, because a wrong email to a recruiter can't be unsent.
- **Email is untrusted input.** Prompts tell the model to ignore instructions inside an email, and model output is validated against a fixed set of categories before it touches the tracker.
- **Minimal scopes:** `gmail.readonly` to read, `gmail.compose` to create drafts.
- **Secrets stay local:** `credentials.json`, `token.json`, `.env`, `profile.json`, `tracker.db` and `logs/` are git-ignored.
- **Monitoring without leaking mail:** the run log and (by default) Langfuse traces hold measurements, not email text.

## Setup

    python -m venv .venv
    .venv\Scripts\pip install -r requirements.txt
    copy .env.example .env              # then put your NVIDIA_API_KEY in .env
    copy profile.example.json profile.json   # then fill in your details

### Google sign-in setup (one time, about 15 minutes)

1. Go to https://console.cloud.google.com and create a project, e.g. `gmail-assistant`.
2. **APIs & Services > Library**: search "Gmail API" and click **Enable**.
3. **Google Auth Platform** (OAuth consent screen): choose **External**, enter an app name and your email.
   Under **Audience > Test users**, add the Gmail address you want to read. Leave the app in **Testing**.
4. **Clients > Create client**: type **Desktop app**. Download the JSON and save it in this folder as `credentials.json`.
5. Run `python -m app.cli auth`. A browser opens: sign in, and on "Google hasn't verified this app"
   click **Continue** (it is your own app). The token is saved to `token.json`.

In Testing mode Google expires the sign-in after 7 days; when that happens the next command opens the browser again.

## Use

    .venv\Scripts\python -m app.cli sync --days 14     # sort the last 14 days of mail
    .venv\Scripts\python -m app.cli report             # applications + action items
    .venv\Scripts\python -m app.cli draft EMAIL_ID --note "accept the Tuesday slot"
    .venv\Scripts\python -m app.cli done EMAIL_ID      # clear an action item
    .venv\Scripts\python -m app.cli stats              # monitoring: runs, latency, failures, tokens

## Monitoring

Every `sync` and `draft` run is logged to `logs/runs.jsonl`: one line per model call,
per email and per run. The log holds timing, tokens, provider, category and errors, and
never the text of your emails. `stats` summarises it:

    LAST 1 RUNS
      started (UTC)       command   emails  rule  model  failed  avg s  p95 s  tokens  providers
      2026-11-22 06:02:31 sync          39    20     19       0    0.8    1.2   21610  nvidia 19

    ALL RUNS
      emails 39  (rule 20, model 19, failed 0)
      model calls 19, errors 0  (nvidia 19)
      latency avg 0.77 s, p95 1.18 s

That run is real: half the mail was sorted by rule with no model call at all.

### Langfuse (optional)

Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` in `.env` (from your Langfuse project's
settings) and each run is also sent to Langfuse as OpenTelemetry traces, using Langfuse's
OTLP endpoint (`/api/public/otel`): one trace per email, a `generation` span per model
call with model, tokens and latency, failed attempts marked as errors, and each sync
grouped as one session.

By default (`LANGFUSE_SEND_CONTENT=false`) Langfuse receives no email text, prompts or
replies, only the measurements. Set it to `true` to see full prompts and outputs in the
Langfuse UI, knowing that your email content then leaves your computer.

## Tests and evaluation

    .venv\Scripts\python -m pytest                     # 21 tests; no Gmail, API key or Langfuse account needed
    .venv\Scripts\python scripts\evaluate.py           # classifier accuracy on data\labelled_emails.jsonl

`data/labelled_emails.jsonl` is 20 synthetic emails with fictional companies. Add real
(anonymised) examples from your own inbox to measure it properly.

## Not built yet

- A web page for the tracker (for now, the `report` command)
- Running `sync` automatically every morning (Windows Task Scheduler can do this)
- Reminders when an application has had no reply for two weeks
