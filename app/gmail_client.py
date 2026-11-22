"""Thin wrapper over the Gmail API.

It reads mail and creates drafts. It has no method that sends mail, on purpose:
every reply waits in Gmail's Drafts folder until the user sends it themselves.
"""
import base64
import html
import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

# readonly: list and read messages. compose: create drafts.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


@dataclass
class Email:
    id: str
    thread_id: str
    sender: str
    sender_email: str
    to: str
    subject: str
    date: str
    snippet: str
    body: str
    message_id: str


def _b64decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace(" ", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def extract_body(payload: dict) -> str:
    """Plain-text body of a Gmail payload: text/plain parts if any, else text/html converted."""
    plain, htmls = [], []

    def walk(part: dict):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64decode(data))
        elif data and mime == "text/html":
            htmls.append(_b64decode(data))
        for p in part.get("parts") or []:
            walk(p)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if htmls:
        return html_to_text("\n".join(htmls))
    return ""


def parse_message(msg: dict) -> Email:
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    name, addr = parseaddr(headers.get("from", ""))
    return Email(
        id=msg["id"],
        thread_id=msg.get("threadId", ""),
        sender=name or addr,
        sender_email=addr.lower(),
        to=headers.get("to", ""),
        subject=headers.get("subject", ""),
        date=headers.get("date", ""),
        snippet=html.unescape(msg.get("snippet", "")),
        body=extract_body(payload),
        message_id=headers.get("message-id", ""),
    )


def build_reply(original: Email, body: str) -> dict:
    """Request body for drafts.create: a reply in the same thread."""
    m = EmailMessage()
    m["To"] = original.sender_email
    subject = original.subject
    m["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if original.message_id:
        m["In-Reply-To"] = original.message_id
        m["References"] = original.message_id
    m.set_content(body)
    raw = base64.urlsafe_b64encode(m.as_bytes()).decode()
    return {"message": {"raw": raw, "threadId": original.thread_id}}


class GmailClient:
    def __init__(self, service):
        self.svc = service

    @classmethod
    def connect(cls, credentials_path: Path, token_path: Path) -> "GmailClient":
        """Sign in once in the browser; later runs reuse token.json."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception:
                    creds = None  # e.g. a Testing-mode token older than 7 days: sign in again
            if not creds or not creds.valid:
                if not credentials_path.exists():
                    raise FileNotFoundError(
                        f"{credentials_path.name} not found. Follow 'Google sign-in setup' in README.md."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json(), encoding="utf-8")
        return cls(build("gmail", "v1", credentials=creds, cache_discovery=False))

    def my_address(self) -> str:
        return self.svc.users().getProfile(userId="me").execute()["emailAddress"]

    def list_ids(self, query: str, max_results: int = 50) -> list[str]:
        ids, token = [], None
        while len(ids) < max_results:
            r = self.svc.users().messages().list(
                userId="me", q=query, maxResults=min(100, max_results - len(ids)), pageToken=token
            ).execute()
            ids += [m["id"] for m in r.get("messages", [])]
            token = r.get("nextPageToken")
            if not token:
                break
        return ids[:max_results]

    def get(self, msg_id: str) -> Email:
        msg = self.svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
        return parse_message(msg)

    def create_draft(self, original: Email, body: str) -> str:
        draft = self.svc.users().drafts().create(userId="me", body=build_reply(original, body)).execute()
        return draft["id"]
