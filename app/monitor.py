"""Run monitoring.

Every run writes to logs/runs.jsonl: one line per model call, per email and per run,
with timing, tokens, provider and outcome. Email text is never written to this log.

If Langfuse keys are set, the same events are also sent to Langfuse as OpenTelemetry
traces: one trace per email, with a generation span per model call. Email text is
left out of those traces too unless LANGFUSE_SEND_CONTENT=true.
"""
import base64
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.gmail_client import Email

TRACE_NAME = "gmail-assistant"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_tracer():
    """(tracer, provider) sending to Langfuse, or (None, None) when no keys are set."""
    public, secret = os.getenv("LANGFUSE_PUBLIC_KEY"), os.getenv("LANGFUSE_SECRET_KEY")
    if not (public and secret):
        return None, None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
    exporter = OTLPSpanExporter(
        endpoint=f"{host}/api/public/otel/v1/traces",
        headers={"Authorization": f"Basic {auth}", "x-langfuse-ingestion-version": "4"},
        timeout=15,
    )
    provider = TracerProvider(resource=Resource.create({"service.name": TRACE_NAME}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider.get_tracer(TRACE_NAME), provider


class Monitor:
    def __init__(self, log_path: Path, command: str, tracer=None, send_content: bool = False):
        self.log_path = log_path
        self.command = command
        self.tracer = tracer
        self.send_content = send_content
        self.run_id = uuid.uuid4().hex[:12]
        self.task = command
        self.started = time.time()
        self.email: Email | None = None
        self.error: str | None = None
        self._root = None
        self._ctx = None
        self.totals = {"emails": 0, "rule": 0, "model": 0, "failed": 0, "model_calls": 0, "call_errors": 0}
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._write({"event": "run_start", "command": command})

    # ---------- local log ----------

    def _write(self, record: dict) -> None:
        line = json.dumps({"ts": _now(), "run_id": self.run_id, **record}, ensure_ascii=False)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ---------- per-email scope ----------

    def begin_email(self, email: Email, task: str) -> None:
        self.email, self.task = email, task
        if self.tracer:
            from opentelemetry import trace

            self._root = self.tracer.start_span(task, attributes={
                **self._trace_attrs(),
                "langfuse.observation.type": "chain",
                "langfuse.observation.metadata.email_id": email.id,
                "langfuse.observation.metadata.sender_domain": email.sender_email.rpartition("@")[2],
            })
            if self.send_content:
                self._root.set_attribute("langfuse.observation.input", email.subject)
            self._ctx = trace.set_span_in_context(self._root)

    def end_email(self, result: dict | None = None, error: str | None = None) -> None:
        source = (result or {}).get("source", "")
        self.totals["emails"] += 1
        if error:
            self.totals["failed"] += 1
        elif source in ("rule", "model"):
            self.totals[source] += 1
        self._write({
            "event": "email", "task": self.task, "email_id": self.email.id if self.email else None,
            "ok": error is None, "source": source, "category": (result or {}).get("category"), "error": error,
        })
        if self._root is not None:
            from opentelemetry.trace import Status, StatusCode

            self._root.set_attribute("langfuse.observation.metadata.source", source or "none")
            if result:
                self._root.set_attribute("langfuse.observation.metadata.category", result.get("category", ""))
                if self.send_content:
                    self._root.set_attribute("langfuse.observation.output", json.dumps(result, ensure_ascii=False))
            if error:
                self._root.set_status(Status(StatusCode.ERROR, error[:300]))
                self._root.set_attribute("langfuse.observation.level", "ERROR")
            self._root.end()
        self.email, self._root, self._ctx = None, None, None

    # ---------- gateway hook ----------

    def on_call(self, call: dict) -> None:
        self.totals["model_calls"] += 1
        if not call["ok"]:
            self.totals["call_errors"] += 1
        latency = (call["end_ns"] - call["start_ns"]) / 1e9
        self._write({
            "event": "model_call", "task": self.task, "email_id": self.email.id if self.email else None,
            "provider": call["provider"], "model": call["model"], "latency_s": round(latency, 3),
            "ok": call["ok"], "error": call["error"],
            "input_tokens": call["input_tokens"], "output_tokens": call["output_tokens"],
        })
        if self.tracer:
            self._span_for_call(call)

    def _span_for_call(self, call: dict) -> None:
        from opentelemetry.trace import Status, StatusCode

        usage = {k: v for k, v in (("input", call["input_tokens"]), ("output", call["output_tokens"])) if v is not None}
        attrs = {
            **self._trace_attrs(),
            "langfuse.observation.type": "generation",
            "langfuse.observation.model.name": call["model"],
            "langfuse.observation.metadata.provider": call["provider"],
        }
        if usage:
            attrs["langfuse.observation.usage_details"] = json.dumps(usage)
        if self.send_content:
            attrs["langfuse.observation.input"] = json.dumps(
                [{"role": "system", "content": call["system"]}, {"role": "user", "content": call["input"]}],
                ensure_ascii=False)
            if call["output"]:
                attrs["langfuse.observation.output"] = call["output"]
        span = self.tracer.start_span(f"{self.task}-llm", context=self._ctx, start_time=call["start_ns"],
                                      attributes=attrs)
        if not call["ok"]:
            span.set_status(Status(StatusCode.ERROR, (call["error"] or "")[:300]))
            span.set_attribute("langfuse.observation.level", "ERROR")
            span.set_attribute("langfuse.observation.status_message", (call["error"] or "")[:300])
        span.end(end_time=call["end_ns"])

    def _trace_attrs(self) -> dict:
        # Trace-level attributes go on every span so Langfuse can filter on them.
        return {
            "langfuse.trace.name": f"{TRACE_NAME}-{self.task}",
            "langfuse.session.id": self.run_id,
            "langfuse.trace.tags": [TRACE_NAME, self.task],
        }

    def set_total(self, total: int) -> None:
        """How many items this run will process, so a dashboard can show live progress."""
        self._write({"event": "run_progress", "total": total})

    def finish(self) -> None:
        self._write({"event": "run_end", "command": self.command, "ok": self.error is None,
                     "error": self.error, "duration_s": round(time.time() - self.started, 1), **self.totals})


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))]


def summarize(log_path: Path, last: int = 10) -> dict:
    """Per-run and overall numbers from logs/runs.jsonl."""
    runs: dict[str, dict] = {}
    calls: list[dict] = []
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            run = runs.setdefault(rec["run_id"], {"run_id": rec["run_id"], "started": rec["ts"], "command": "",
                                                  "emails": 0, "rule": 0, "model": 0, "failed": 0, "latencies": [],
                                                  "providers": {}, "call_errors": 0, "tokens": 0})
            ev = rec["event"]
            if ev == "run_start":
                run["command"] = rec["command"]
            elif ev == "email":
                run["emails"] += 1
                if not rec["ok"]:
                    run["failed"] += 1
                elif rec["source"] in ("rule", "model"):
                    run[rec["source"]] += 1
            elif ev == "model_call":
                calls.append(rec)
                run["providers"][rec["provider"]] = run["providers"].get(rec["provider"], 0) + 1
                if rec["ok"]:
                    run["latencies"].append(rec["latency_s"])
                else:
                    run["call_errors"] += 1
                run["tokens"] += (rec.get("input_tokens") or 0) + (rec.get("output_tokens") or 0)
    ok_latencies = [c["latency_s"] for c in calls if c["ok"]]
    overall = {
        "runs": len(runs),
        "emails": sum(r["emails"] for r in runs.values()),
        "rule": sum(r["rule"] for r in runs.values()),
        "model": sum(r["model"] for r in runs.values()),
        "failed": sum(r["failed"] for r in runs.values()),
        "model_calls": len(calls),
        "call_errors": sum(1 for c in calls if not c["ok"]),
        "providers": {},
        "avg_latency_s": round(sum(ok_latencies) / len(ok_latencies), 2) if ok_latencies else None,
        "p95_latency_s": round(_percentile(ok_latencies, 95), 2) if ok_latencies else None,
        "tokens": sum(r["tokens"] for r in runs.values()),
    }
    for c in calls:
        overall["providers"][c["provider"]] = overall["providers"].get(c["provider"], 0) + 1
    return {"runs": list(runs.values())[-last:], "overall": overall}
