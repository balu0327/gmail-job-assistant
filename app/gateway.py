"""Model gateway: one place that talks to LLMs.

Every provider is an OpenAI-compatible /chat/completions endpoint. Providers are
tried in order, so a rate-limited hosted endpoint falls through to the local one.
"""
import json
import re

import httpx


class ModelError(Exception):
    pass


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (handles code fences and chatter)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in reply")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError("reply JSON is not an object")
    return obj


class Gateway:
    def __init__(self, providers: list[dict], client: httpx.Client | None = None):
        self.providers = providers
        # Free endpoints can queue a request for minutes; fail fast and fall through instead.
        self.http = client or httpx.Client(timeout=httpx.Timeout(60, connect=10))
        self.last_model = None

    # Reasoning models (gpt-oss) spend tokens thinking before the answer, so limits are generous.
    def chat(self, system: str, user: str, temperature: float = 0.2, max_tokens: int = 4096) -> str:
        if not self.providers:
            raise ModelError("No model provider configured. Set NVIDIA_API_KEY or OLLAMA_MODEL in .env.")
        errors = []
        for p in self.providers:
            try:
                r = self.http.post(
                    p["base_url"].rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {p['api_key']}"},
                    json={
                        "model": p["model"],
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        **p.get("extra", {}),
                    },
                )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"].get("content")
                if not content or not content.strip():
                    raise ValueError("empty reply")
                self.last_model = f"{p['name']}:{p['model']}"
                return content.strip()
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                errors.append(f"{p['name']}: {type(e).__name__}: {e}")
        raise ModelError("All providers failed. " + " | ".join(errors))

    def chat_json(self, system: str, user: str, retries: int = 1) -> dict:
        last = None
        for _ in range(retries + 1):
            reply = self.chat(system, user, temperature=0.0)
            try:
                return extract_json(reply)
            except ValueError as e:
                last = e
        raise ModelError(f"Model did not return valid JSON: {last}")
