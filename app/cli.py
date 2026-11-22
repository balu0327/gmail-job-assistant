"""Command line for the Gmail job-search assistant.

    python -m app.cli auth                 sign in to Gmail once
    python -m app.cli sync [--days 14]     read new mail, sort it, update the tracker
    python -m app.cli report               applications and mail that needs you
    python -m app.cli draft EMAIL_ID       write a reply and save it as a Gmail draft (never sends)
    python -m app.cli done EMAIL_ID        mark an action item as handled
"""
import argparse
import sys
import time

from app import config
from app.classify import classify
from app.drafts import draft_reply, placeholders
from app.gateway import Gateway, ModelError
from app.gmail_client import GmailClient
from app.tracker import Tracker

LABELS = {
    "offer": "OFFER", "interview": "INTERVIEW", "assessment": "TEST", "rejection": "REJECTED",
    "application_received": "RECEIVED", "recruiter_outreach": "RECRUITER", "job_alert": "alert", "other": "other",
}


def gmail() -> GmailClient:
    return GmailClient.connect(config.CREDENTIALS, config.TOKEN)


def cmd_auth(args):
    print(f"Signed in as {gmail().my_address()}")


def cmd_sync(args):
    gm, tracker, gw = gmail(), Tracker(str(config.TRACKER_DB)), Gateway(config.providers())
    query = f"newer_than:{args.days}d -in:sent -in:chats -in:drafts"
    ids = [i for i in gm.list_ids(query, args.max) if not tracker.seen(i)]
    print(f"{len(ids)} new emails to sort")
    failed = 0
    for n, msg_id in enumerate(ids, 1):
        email = gm.get(msg_id)
        try:
            result = classify(gw, email)
        except ModelError as e:
            failed += 1  # not recorded, so the next sync tries it again
            print(f"[{n}/{len(ids)}] FAILED {email.subject[:60]}  ({e})")
            continue
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
    gm, gw = gmail(), Gateway(config.providers())
    email = gm.get(args.email_id)
    reply = draft_reply(gw, email, config.load_profile(), args.note)
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


def cmd_done(args):
    ok = Tracker(str(config.TRACKER_DB)).mark_handled(args.email_id)
    print("Marked as handled." if ok else "No email with that id in the tracker.")


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # subjects carry emoji
    ap = argparse.ArgumentParser(prog="python -m app.cli", description="Gmail job-search assistant")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="sign in to Gmail").set_defaults(fn=cmd_auth)
    s = sub.add_parser("sync", help="sort new mail and update the tracker")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--max", type=int, default=100)
    s.add_argument("--delay", type=float, default=1.0, help="seconds between model calls")
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
    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except (ModelError, FileNotFoundError) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
