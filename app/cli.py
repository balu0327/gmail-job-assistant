"""Command line for the Gmail job-search assistant.

    python -m app.cli auth                 sign in to Gmail once
    python -m app.cli sync [--days 14]     read new mail, sort it, update the tracker
    python -m app.cli report               applications and mail that needs you
    python -m app.cli draft EMAIL_ID       write a reply and save it as a Gmail draft (never sends)
    python -m app.cli done EMAIL_ID        mark an action item as handled
    python -m app.cli stats                model calls, latency, failures per run
"""
import argparse
import sys
import time
from contextlib import contextmanager

from app import config
from app.classify import classify
from app.drafts import draft_reply, placeholders
from app.gateway import Gateway, ModelError
from app.gmail_client import GmailClient, SignInRequired
from app.monitor import Monitor, make_tracer, summarize
from app.status import write_status
from app.tracker import Tracker

LABELS = {
    "offer": "OFFER", "interview": "INTERVIEW", "assessment": "TEST", "rejection": "REJECTED",
    "application_received": "RECEIVED", "recruiter_outreach": "RECRUITER", "job_alert": "alert", "other": "other",
}


def gmail(interactive: bool = True) -> GmailClient:
    return GmailClient.connect(config.CREDENTIALS, config.TOKEN, interactive)


@contextmanager
def monitored(command: str):
    """A Monitor for one run, plus a Gateway that reports every model call to it."""
    tracer, provider = make_tracer()
    monitor = Monitor(config.RUN_LOG, command, tracer, config.langfuse_send_content())
    try:
        yield monitor, Gateway(config.providers(), on_call=monitor.on_call)
    finally:
        monitor.finish()
        if provider:
            provider.force_flush()
            provider.shutdown()


def cmd_auth(args):
    print(f"Signed in as {gmail().my_address()}")


def cmd_sync(args):
    with monitored("sync") as (monitor, gw):
        try:
            _sync(args, monitor, gw)
        except Exception as e:
            monitor.error = f"{type(e).__name__}: {e}"[:500]
            raise
        finally:
            # Refresh the dashboard snapshot whether the run worked or not.
            write_status(config.STATUS_FILE, Tracker(str(config.TRACKER_DB)), monitor.error)


def _sync(args, monitor: Monitor, gw: Gateway):
    gm, tracker = gmail(interactive=not args.scheduled), Tracker(str(config.TRACKER_DB))
    query = f"newer_than:{args.days}d -in:sent -in:chats -in:drafts"
    ids = [i for i in gm.list_ids(query, args.max) if not tracker.seen(i)]
    monitor.set_total(len(ids))
    print(f"{len(ids)} new emails to sort")
    failed = 0
    for n, msg_id in enumerate(ids, 1):
        email = gm.get(msg_id)
        monitor.begin_email(email, "classify")
        try:
            result = classify(gw, email)
        except ModelError as e:
            failed += 1  # not recorded, so the next sync tries it again
            monitor.end_email(error=str(e))
            print(f"[{n}/{len(ids)}] FAILED {email.subject[:60]}  ({e})")
            continue
        monitor.end_email(result)
        tracker.record(email, result)
        flag = " *ACTION*" if result["action_needed"] else ""
        print(f"[{n}/{len(ids)}] {LABELS[result['category']]:<9}{flag} {email.subject[:70]}")
        if result["source"] == "model" and args.delay:
            time.sleep(args.delay)  # free endpoints are rate-limited
    if failed:
        print(f"\n{failed} emails failed and will be retried on the next sync.")
    print()
    cmd_report(args, tracker)


def cmd_report(args, tracker: Tracker | None = None):
    tracker = tracker or Tracker(str(config.TRACKER_DB))
    apps = tracker.applications()
    print("APPLICATIONS")
    if not apps:
        print("  none yet")
    for a in apps:
        role = f" - {a['role']}" if a["role"] else ""
        print(f"  {LABELS[a['status']]:<9} {a['company']}{role}  (updated {a['updated'][:10]})")
    pending = tracker.pending()
    print("\nNEEDS YOUR ACTION")
    if not pending:
        print("  nothing")
    for p in pending:
        due = f" by {p['deadline']}" if p["deadline"] else ""
        print(f"  [{p['id']}] {LABELS[p['category']]}{due}: {p['summary'] or p['subject']}")
    counts = tracker.counts()
    if counts:
        print("\nSORTED SO FAR: " + ", ".join(f"{LABELS.get(k, k)} {v}" for k, v in sorted(counts.items())))


def cmd_draft(args):
    gm = gmail()
    email = gm.get(args.email_id)
    with monitored("draft") as (monitor, gw):
        monitor.begin_email(email, "draft")
        try:
            reply = draft_reply(gw, email, config.load_profile(), args.note)
        except ModelError as e:
            monitor.end_email(error=str(e))
            raise
        monitor.end_email({"source": "model", "category": "draft"})
    print(f"Reply to: {email.sender} <{email.sender_email}>\nSubject:  Re: {email.subject}\n")
    print(reply)
    gaps = placeholders(reply)
    if gaps:
        print("\nFill these in before sending: " + ", ".join(gaps))
    if not args.yes and input("\nSave this as a Gmail draft? [y/N] ").strip().lower() != "y":
        print("Not saved.")
        return
    draft_id = gm.create_draft(email, reply)
    print(f"Saved as draft {draft_id}. It is in Gmail > Drafts; nothing was sent.")


def cmd_stats(args):
    s = summarize(config.RUN_LOG, args.last)
    o = s["overall"]
    if not o["runs"]:
        print("No runs logged yet. Run sync first.")
        return
    print(f"LAST {len(s['runs'])} RUNS")
    print(f"  {'started (UTC)':<20}{'command':<9}{'emails':>7}{'rule':>6}{'model':>7}{'failed':>8}"
          f"{'avg s':>7}{'p95 s':>7}{'tokens':>8}  providers")
    for r in s["runs"]:
        lat = r["latencies"]
        avg = f"{sum(lat) / len(lat):.1f}" if lat else "-"
        p95 = f"{sorted(lat)[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))]:.1f}" if lat else "-"
        providers = ", ".join(f"{k} {v}" for k, v in r["providers"].items()) or "-"
        print(f"  {r['started'][:19].replace('T', ' '):<20}{r['command']:<9}{r['emails']:>7}{r['rule']:>6}"
              f"{r['model']:>7}{r['failed']:>8}{avg:>7}{p95:>7}{r['tokens']:>8}  {providers}")
    print("\nALL RUNS")
    print(f"  emails {o['emails']}  (rule {o['rule']}, model {o['model']}, failed {o['failed']})")
    print(f"  model calls {o['model_calls']}, errors {o['call_errors']}"
          f"  ({', '.join(f'{k} {v}' for k, v in o['providers'].items()) or 'none'})")
    if o["avg_latency_s"] is not None:
        print(f"  latency avg {o['avg_latency_s']} s, p95 {o['p95_latency_s']} s")
    print(f"  tokens {o['tokens']}")
    print(f"\nLog file: {config.RUN_LOG}")


def cmd_done(args):
    tracker = Tracker(str(config.TRACKER_DB))
    ok = tracker.mark_handled(args.email_id)
    print("Marked as handled." if ok else "No email with that id in the tracker.")
    if ok:
        write_status(config.STATUS_FILE, tracker)


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # subjects carry emoji
    ap = argparse.ArgumentParser(prog="python -m app.cli", description="Gmail job-search assistant")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="sign in to Gmail").set_defaults(fn=cmd_auth)
    s = sub.add_parser("sync", help="sort new mail and update the tracker")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--max", type=int, default=100)
    s.add_argument("--delay", type=float, default=1.0, help="seconds between model calls")
    s.add_argument("--scheduled", action="store_true",
                   help="unattended run: never open a browser; fail with a message if sign-in expired")
    s.set_defaults(fn=cmd_sync)
    sub.add_parser("report", help="show applications and action items").set_defaults(fn=lambda a: cmd_report(a))
    d = sub.add_parser("draft", help="draft a reply (saved to Gmail Drafts, never sent)")
    d.add_argument("email_id")
    d.add_argument("--note", default="", help='e.g. "accept the Tuesday slot"')
    d.add_argument("--yes", action="store_true", help="save the draft without asking")
    d.set_defaults(fn=cmd_draft)
    x = sub.add_parser("done", help="mark an action item as handled")
    x.add_argument("email_id")
    x.set_defaults(fn=cmd_done)
    st = sub.add_parser("stats", help="model calls, latency and failures per run")
    st.add_argument("--last", type=int, default=10, help="how many recent runs to list")
    st.set_defaults(fn=cmd_stats)
    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except (ModelError, FileNotFoundError, SignInRequired) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
