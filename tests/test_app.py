import base64
import email as email_lib

from app.classify import BULK_SENDERS, classify, validate
from app.drafts import draft_reply, placeholders
from app.gateway import extract_json
from app.gmail_client import Email, GmailClient, build_reply, extract_body, parse_message
from app.tracker import Tracker


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def make_email(**kw) -> Email:
    base = dict(id="m1", thread_id="t1", sender="Asha (Nimbus Labs)", sender_email="asha@nimbuslabs.example",
                to="me@example.com", subject="Interview for Software Engineer Intern",
                date="Tue, 17 Nov 2026 10:00:00 +0530", snippet="", body="Please share your slots.",
                message_id="<abc@nimbuslabs.example>")
    base.update(kw)
    return Email(**base)


class FakeGateway:
    def __init__(self, reply=None, text="Hi Asha,\nThank you. I am available [your available time slots].\nBalu"):
        self.reply, self.text, self.json_calls, self.chat_calls = reply, text, 0, []

    def chat_json(self, system, user, retries=1):
        self.json_calls += 1
        return self.reply

    def chat(self, system, user, **kw):
        self.chat_calls.append(user)
        return self.text


# ---------- reading Gmail messages ----------

def test_parse_multipart_prefers_plain_text():
    msg = {"id": "x", "threadId": "t", "snippet": "Hi &amp; welcome", "payload": {
        "headers": [{"name": "From", "value": "Nimbus HR <HR@Nimbus.example>"},
                    {"name": "Subject", "value": "Hello"},
                    {"name": "Message-ID", "value": "<id1@nimbus>"}],
        "mimeType": "multipart/alternative",
        "parts": [{"mimeType": "text/html", "body": {"data": b64("<p>HTML version</p>")}},
                  {"mimeType": "text/plain", "body": {"data": b64("Plain version")}}]}}
    e = parse_message(msg)
    assert e.body == "Plain version"
    assert e.sender == "Nimbus HR" and e.sender_email == "hr@nimbus.example"
    assert e.snippet == "Hi & welcome" and e.message_id == "<id1@nimbus>"


def test_html_only_body_is_converted_to_text():
    payload = {"mimeType": "text/html", "body": {"data": b64(
        "<style>p{color:red}</style><p>Your test link</p><br><a href='x'>Start&nbsp;test</a>")}}
    text = extract_body(payload)
    assert "Your test link" in text and "Start test" in text and "color" not in text


# ---------- classification ----------

def test_bulk_sender_is_labelled_without_a_model_call():
    gw = FakeGateway()
    result = classify(gw, make_email(sender_email=next(iter(BULK_SENDERS))))
    assert result["category"] == "job_alert" and result["source"] == "rule"
    assert gw.json_calls == 0


def test_bulk_domain_walk_in_invite_is_an_alert_not_an_interview():
    gw = FakeGateway({"category": "interview"})
    result = classify(gw, make_email(sender_email="harshidgvjag5vy3@naukri.com",
                                     subject="Walk-in interview | Hiring for day shift"))
    assert result["category"] == "job_alert" and gw.json_calls == 0


def test_model_result_is_used_and_marked():
    gw = FakeGateway({"category": "interview", "company": "Nimbus Labs", "role": "Software Engineer Intern",
                      "action_needed": True, "deadline": "2026-11-20", "summary": "Book an interview slot."})
    result = classify(gw, make_email())
    assert result["category"] == "interview" and result["action_needed"] is True
    assert result["deadline"] == "2026-11-20" and result["source"] == "model"


def test_validate_rejects_bad_model_output():
    r = validate({"category": "party_invite", "company": "  ", "role": None, "action_needed": "yes",
                  "deadline": "next Friday", "summary": 42})
    assert r == {"category": "other", "company": None, "role": None, "action_needed": False,
                 "deadline": None, "summary": ""}


def test_extract_json_handles_fences_and_thinking():
    assert extract_json('<think>hmm</think>```json\n{"category": "offer"}\n```') == {"category": "offer"}


# ---------- tracker ----------

def result(category, company="Nimbus Labs", role="Software Engineer Intern", action=False):
    return {"category": category, "company": company, "role": role, "action_needed": action,
            "deadline": None, "summary": category, "source": "model"}


def test_status_moves_forward_only():
    t = Tracker()
    t.record(make_email(id="a", date="Mon, 10 Nov 2026 10:00:00 +0530"), result("application_received"))
    t.record(make_email(id="b", date="Wed, 12 Nov 2026 10:00:00 +0530"), result("interview", action=True))
    t.record(make_email(id="c", date="Thu, 13 Nov 2026 10:00:00 +0530"), result("application_received"))
    apps = t.applications()
    assert len(apps) == 1 and apps[0]["status"] == "interview"
    assert t.seen("c") and not t.seen("zzz")


def test_company_only_mail_updates_latest_application_and_rejection_is_final():
    t = Tracker()
    t.record(make_email(id="a", date="Mon, 10 Nov 2026 10:00:00 +0530"), result("application_received"))
    t.record(make_email(id="b", date="Tue, 11 Nov 2026 10:00:00 +0530"), result("rejection", company="NIMBUS LABS.", role=None))
    t.record(make_email(id="c", date="Wed, 12 Nov 2026 10:00:00 +0530"), result("assessment", role=None))
    apps = t.applications()
    assert len(apps) == 1 and apps[0]["status"] == "rejection"


def test_alerts_and_unknown_companies_do_not_create_applications():
    t = Tracker()
    t.record(make_email(id="a"), result("job_alert", company=None))
    t.record(make_email(id="b"), result("interview", company=None))
    assert t.applications() == [] and t.counts() == {"job_alert": 1, "interview": 1}


def test_pending_actions_and_mark_handled():
    t = Tracker()
    t.record(make_email(id="a"), result("assessment", action=True))
    t.record(make_email(id="b"), result("application_received"))
    assert [p["id"] for p in t.pending()] == ["a"]
    assert t.mark_handled("a") and t.pending() == []
    assert not t.mark_handled("missing")


# ---------- drafting replies ----------

def test_draft_uses_profile_and_flags_placeholders():
    gw = FakeGateway()
    text = draft_reply(gw, make_email(), {"name": "Balu"}, note="keep it short")
    assert '"name": "Balu"' in gw.chat_calls[0] and "keep it short" in gw.chat_calls[0]
    assert placeholders(text) == ["[your available time slots]"]


def test_reply_threads_with_original():
    req = build_reply(make_email(), "Thanks!")
    assert req["message"]["threadId"] == "t1"
    raw = base64.urlsafe_b64decode(req["message"]["raw"])
    msg = email_lib.message_from_bytes(raw)
    assert msg["To"] == "asha@nimbuslabs.example"
    assert msg["Subject"] == "Re: Interview for Software Engineer Intern"
    assert msg["In-Reply-To"] == "<abc@nimbuslabs.example>"
    assert "Thanks!" in msg.get_payload()


def test_existing_re_prefix_is_not_doubled():
    req = build_reply(make_email(subject="RE: Interview"), "ok")
    msg = email_lib.message_from_bytes(base64.urlsafe_b64decode(req["message"]["raw"]))
    assert msg["Subject"] == "RE: Interview"


class FakeService:
    """Records Gmail API calls; drafts().create is the only write it supports."""
    def __init__(self):
        self.created = []

    def users(self):
        return self

    def drafts(self):
        return self

    def create(self, userId, body):
        self.created.append(body)
        return self

    def execute(self):
        return {"id": "d1"}


def test_client_creates_draft_and_cannot_send():
    svc = FakeService()
    client = GmailClient(svc)
    assert client.create_draft(make_email(), "Hello") == "d1"
    assert len(svc.created) == 1
    assert not any("send" in name.lower() for name in dir(GmailClient))


# ---------- dashboard status and unattended runs ----------

def test_status_snapshot_for_dashboard(tmp_path):
    import json as _json
    from app.status import build_status, write_status

    t = Tracker()
    t.record(make_email(id="a", thread_id="th1"), result("interview", action=True))
    t.record(make_email(id="b"), result("job_alert", company=None))
    s = build_status(t)
    assert s["state"] == "ok" and s["attention"] == 1
    metrics = {m["label"]: m["value"] for m in s["metrics"]}
    assert metrics["Applications"] == 1 and metrics["Tests & interviews"] == 1 and metrics["Emails sorted"] == 2
    action = s["sections"][0]["items"][0]
    assert action["badge"] == "Interview" and action["link"].endswith("/th1") and "draft a" in action["hint"]
    assert len(s["sections"][2]["rows"]) == 1  # alerts are left out of "Recent job mail"
    path = tmp_path / "status" / "status.json"
    write_status(path, t, error="SignInRequired: expired")
    saved = _json.loads(path.read_text(encoding="utf-8"))
    assert saved["state"] == "error" and "expired" in saved["message"]


def test_unattended_connect_never_opens_a_browser(tmp_path):
    import pytest as _pytest
    from app.gmail_client import SignInRequired

    with _pytest.raises(SignInRequired):
        GmailClient.connect(tmp_path / "credentials.json", tmp_path / "token.json", interactive=False)


def test_action_items_show_when_sent_and_can_be_cleared():
    from app.status import build_status

    t = Tracker()
    t.record(make_email(id="x1", date="Sat, 28 Nov 2026 10:46:00 +0530"), result("offer", action=True))
    item = build_status(t)["sections"][0]["items"][0]
    assert item["id"] == "x1" and item["clearable"] is True
    assert "sent Sat 28 Nov" in item["subtitle"]
    assert t.mark_handled("x1") and build_status(t)["sections"][0]["items"] == []
