"""Status snapshot for dashboards (Agent Hub status format, version 1).

After every run the agent writes status/status.json: what it is, whether its last run
worked, headline numbers, and sections (lists and tables) for a dashboard to render.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.tracker import Tracker

LABELS = {
    "offer": "Offer", "interview": "Interview", "assessment": "Test", "rejection": "Rejected",
    "application_received": "Received", "recruiter_outreach": "Recruiter", "job_alert": "Alert", "other": "Other",
}
TONES = {"offer": "good", "interview": "warn", "assessment": "warn", "rejection": "bad"}


def _gmail_link(thread_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}" if thread_id else ""


def build_status(tracker: Tracker, error: str | None = None) -> dict:
    apps = tracker.applications()
    pending = tracker.pending()
    active = sum(1 for a in apps if a["status"] in ("assessment", "interview"))
    recent = tracker.db.execute(
        "SELECT * FROM emails WHERE category NOT IN ('job_alert', 'other') ORDER BY received DESC LIMIT 15"
    ).fetchall()
    counts = tracker.counts()
    return {
        "format": 1,
        "agent": "gmail-assistant",
        "name": "Gmail Job Assistant",
        "description": "Sorts job-search mail, tracks applications and drafts replies (never sends).",
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "state": "error" if error else "ok",
        "message": error or "",
        "metrics": [
            {"label": "Applications", "value": len(apps)},
            {"label": "Tests & interviews", "value": active, "tone": "warn" if active else None},
            {"label": "Offers", "value": sum(1 for a in apps if a["status"] == "offer"), "tone": "good"},
            {"label": "Needs action", "value": len(pending), "tone": "warn" if pending else None},
            {"label": "Emails sorted", "value": sum(counts.values())},
        ],
        "attention": len(pending),
        "sections": [
            {
                "title": "Needs your action",
                "type": "list",
                "focus": True,  # dashboards show these items first
                "empty": "Nothing needs you right now.",
                "items": [{
                    "title": p["summary"] or p["subject"],
                    "subtitle": f"{p['sender']}" + (f" · due {p['deadline']}" if p["deadline"] else ""),
                    "badge": LABELS.get(p["category"], p["category"]),
                    "tone": TONES.get(p["category"], "warn"),
                    "link": _gmail_link(p["thread_id"]),
                    "hint": f"python -m app.cli draft {p['id']}",
                } for p in pending],
            },
            {
                "title": "Applications",
                "type": "table",
                "empty": "No applications tracked yet.",
                "columns": ["Status", "Company", "Role", "Last update"],
                "rows": [[
                    {"text": LABELS.get(a["status"], a["status"]), "tone": TONES.get(a["status"])},
                    a["company"], a["role"] or "", a["updated"][:10],
                ] for a in apps],
            },
            {
                "title": "Recent job mail",
                "type": "table",
                "empty": "No job mail yet.",
                "columns": ["Received", "Type", "Subject"],
                "rows": [[
                    r["received"][:10],
                    {"text": LABELS.get(r["category"], r["category"]), "tone": TONES.get(r["category"])},
                    {"text": r["subject"], "link": _gmail_link(r["thread_id"])},
                ] for r in recent],
            },
        ],
    }


def write_status(path: Path, tracker: Tracker, error: str | None = None) -> None:
    """Write atomically so a dashboard never reads a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(build_status(tracker, error), ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
