import json

import httpx
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.gateway import Gateway
from app.monitor import Monitor, summarize
from tests.test_app import make_email

PROVIDERS = [
    {"name": "nvidia", "base_url": "https://nvidia.example/v1", "api_key": "k", "model": "big-model"},
    {"name": "ollama", "base_url": "http://localhost:11434/v1", "api_key": "o", "model": "small-model"},
]
OK_REPLY = {"choices": [{"message": {"content": '{"category": "interview"}'}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 15}}


def fallback_client():
    """First provider fails with 500, the local one answers."""
    def handler(request):
        if "nvidia" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, json=OK_REPLY)
    return httpx.Client(transport=httpx.MockTransport(handler))


def in_memory_tracer():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def run_one(tmp_path, tracer=None, send_content=False):
    monitor = Monitor(tmp_path / "logs" / "runs.jsonl", "sync", tracer, send_content)
    gw = Gateway(PROVIDERS, fallback_client(), on_call=monitor.on_call)
    email = make_email(body="SECRET EMAIL TEXT")
    monitor.begin_email(email, "classify")
    gw.chat_json("system prompt", "user prompt with SECRET EMAIL TEXT")
    monitor.end_email({"category": "interview", "source": "model"})
    monitor.finish()
    return monitor


def test_gateway_reports_every_attempt_including_fallback():
    calls = []
    Gateway(PROVIDERS, fallback_client(), on_call=calls.append).chat("s", "u")
    assert [(c["provider"], c["ok"]) for c in calls] == [("nvidia", False), ("ollama", True)]
    assert "500" in calls[0]["error"]
    assert calls[1]["input_tokens"] == 120 and calls[1]["output_tokens"] == 15
    assert all(c["end_ns"] >= c["start_ns"] for c in calls)


def test_local_log_has_metrics_but_no_email_text(tmp_path):
    run_one(tmp_path)
    text = (tmp_path / "logs" / "runs.jsonl").read_text(encoding="utf-8")
    assert "SECRET" not in text and "user prompt" not in text
    events = [json.loads(line)["event"] for line in text.splitlines()]
    assert events == ["run_start", "model_call", "model_call", "email", "run_end"]


def test_summarize_counts_runs_providers_and_failures(tmp_path):
    run_one(tmp_path)
    run_one(tmp_path)
    s = summarize(tmp_path / "logs" / "runs.jsonl")
    o = s["overall"]
    assert o["runs"] == 2 and o["emails"] == 2 and o["model"] == 2 and o["failed"] == 0
    assert o["model_calls"] == 4 and o["call_errors"] == 2
    assert o["providers"] == {"nvidia": 2, "ollama": 2}
    assert o["tokens"] == 270 and o["avg_latency_s"] is not None


def test_summarize_with_no_log_file(tmp_path):
    assert summarize(tmp_path / "missing.jsonl")["overall"]["runs"] == 0


def test_langfuse_spans_are_nested_and_redacted_by_default(tmp_path):
    tracer, exporter = in_memory_tracer()
    monitor = run_one(tmp_path, tracer)
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert set(spans) == {"classify", "classify-llm"}
    root = spans["classify"]
    gens = [s for s in exporter.get_finished_spans() if s.name == "classify-llm"]
    assert len(gens) == 2 and all(g.parent.span_id == root.context.span_id for g in gens)
    ok = [g for g in gens if g.attributes["langfuse.observation.metadata.provider"] == "ollama"][0]
    failed = [g for g in gens if g.attributes["langfuse.observation.metadata.provider"] == "nvidia"][0]
    assert ok.attributes["langfuse.observation.type"] == "generation"
    assert ok.attributes["langfuse.observation.model.name"] == "small-model"
    assert json.loads(ok.attributes["langfuse.observation.usage_details"]) == {"input": 120, "output": 15}
    assert ok.attributes["langfuse.session.id"] == monitor.run_id
    assert failed.attributes["langfuse.observation.level"] == "ERROR"
    assert root.attributes["langfuse.observation.metadata.category"] == "interview"
    for span in exporter.get_finished_spans():
        assert "langfuse.observation.input" not in span.attributes
        assert "langfuse.observation.output" not in span.attributes
        assert not any("SECRET" in str(v) for v in span.attributes.values())


def test_langfuse_content_only_when_switched_on(tmp_path):
    tracer, exporter = in_memory_tracer()
    run_one(tmp_path, tracer, send_content=True)
    gen = [s for s in exporter.get_finished_spans()
           if s.attributes.get("langfuse.observation.metadata.provider") == "ollama"][0]
    assert "SECRET EMAIL TEXT" in gen.attributes["langfuse.observation.input"]
    assert gen.attributes["langfuse.observation.output"] == '{"category": "interview"}'
