"""SQLite store: every processed email, plus one row per application with its latest status."""
import sqlite3
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from app.gmail_client import Email

# Application status only moves forward: a late "we received your application" mail
# never downgrades an interview. Rejection is final.
STATUS_RANK = {"application_received": 1, "assessment": 2, "interview": 3, "offer": 4, "rejection": 5}

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id TEXT PRIMARY KEY,
    thread_id TEXT,
    received TEXT,
    sender TEXT,
    sender_email TEXT,
    subject TEXT,
    category TEXT,
    company TEXT,
    role TEXT,
    action_needed INTEGER,
    deadline TEXT,
    summary TEXT,
    source TEXT,
    handled INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT,
    role TEXT,
    company_key TEXT,
    role_key TEXT,
    status TEXT,
    updated TEXT,
    last_email_id TEXT
);
"""


def iso_date(raw: str) -> str:
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat(timespec="minutes")
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat(timespec="minutes")


def _key(s: str | None) -> str:
    return " ".join((s or "").lower().replace(".", " ").split())


class Tracker:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def seen(self, email_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM emails WHERE id = ?", (email_id,)).fetchone() is not None

    def record(self, email: Email, result: dict) -> None:
        received = iso_date(email.date)
        self.db.execute(
            "INSERT OR REPLACE INTO emails (id, thread_id, received, sender, sender_email, subject, category,"
            " company, role, action_needed, deadline, summary, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (email.id, email.thread_id, received, email.sender, email.sender_email, email.subject,
             result["category"], result["company"], result["role"], int(result["action_needed"]),
             result["deadline"], result["summary"], result.get("source", "")),
        )
        if result["category"] in STATUS_RANK and result["company"]:
            self._update_application(result["company"], result["role"], result["category"], received, email.id)
        self.db.commit()

    def _find(self, company_key: str, role_key: str):
        row = self.db.execute(
            "SELECT * FROM applications WHERE company_key = ? AND role_key = ?", (company_key, role_key)
        ).fetchone()
        if row or role_key:
            return row
        # Mail that names only the company updates that company's most recent application.
        return self.db.execute(
            "SELECT * FROM applications WHERE company_key = ? ORDER BY updated DESC LIMIT 1", (company_key,)
        ).fetchone()

    def _update_application(self, company, role, status, when, email_id):
        ck, rk = _key(company), _key(role)
        row = self._find(ck, rk)
        if row is None:
            self.db.execute(
                "INSERT INTO applications (company, role, company_key, role_key, status, updated, last_email_id)"
                " VALUES (?,?,?,?,?,?,?)",
                (company, role, ck, rk, status, when, email_id),
            )
        elif STATUS_RANK[status] >= STATUS_RANK[row["status"]] and when >= row["updated"]:
            self.db.execute(
                "UPDATE applications SET status = ?, updated = ?, last_email_id = ? WHERE id = ?",
                (status, when, email_id, row["id"]),
            )

    def applications(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM applications ORDER BY updated DESC").fetchall()

    def pending(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM emails WHERE action_needed = 1 AND handled = 0"
            " ORDER BY COALESCE(deadline, '9999-12-31'), received DESC"
        ).fetchall()

    def mark_handled(self, email_id: str) -> bool:
        cur = self.db.execute("UPDATE emails SET handled = 1 WHERE id = ?", (email_id,))
        self.db.commit()
        return cur.rowcount > 0

    def counts(self) -> dict:
        rows = self.db.execute("SELECT category, COUNT(*) AS n FROM emails GROUP BY category").fetchall()
        return {r["category"]: r["n"] for r in rows}
