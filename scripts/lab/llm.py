"""Thin OpenAI Responses API client. The key is read from OPENAI_API_KEY only; never stored."""
import json
import os
import time

import httpx

URL = "https://api.openai.com/v1/responses"
USAGE_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".work", "lab", "llm_usage.jsonl")


def ask(prompt: str, model: str = "gpt-6.1-terra", effort: str | None = "high", system: str | None = None,
        extra: dict | None = None, timeout: float = 600) -> str:
    key = os.environ["OPENAI_API_KEY"]
    body: dict = {"model": model, "input": prompt}
    if system:
        body["instructions"] = system
    if effort:
        body["reasoning"] = {"effort": effort}
    if extra:
        body.update(extra)
    t0 = time.perf_counter()
    r = httpx.post(URL, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:500]}")
    data = r.json()
    text = data.get("output_text") or "".join(
        part.get("text", "") for item in data.get("output", []) if item.get("type") == "message"
        for part in item.get("content", []) if part.get("type") in ("output_text", "text"))
    usage = data.get("usage", {})
    with open(USAGE_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"model": model, "effort": effort, "seconds": round(time.perf_counter() - t0, 1),
                             "usage": usage, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}) + "\n")
    return text
