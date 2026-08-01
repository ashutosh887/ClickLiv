"""One optional LLM call, traced to Langfuse. OpenAI when a key is set, else Bedrock,
else a no-op that changes no output (D33).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import otel

OPENAI_URL = "https://api.openai.com/v1/responses"
DEFAULT_OPENAI_MODEL = "gpt-5.2"
BEDROCK_MODEL = "openai.gpt-oss-120b"


def post(url: str, payload: dict, headers: dict, timeout: int = 60) -> dict | None:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        print(f"llm: call failed, {exc.code} {detail}")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"llm: call failed, {exc}")
    return None


def read_responses_api(body: dict) -> tuple[str | None, dict]:
    """Both providers speak the OpenAI Responses shape, so one reader serves both."""
    usage = body.get("usage") or {}
    for item in body.get("output", []):
        if item.get("type") == "message":
            for chunk in item.get("content", []) or []:
                if chunk.get("type") == "output_text":
                    return chunk["text"].strip(), usage
    return None, usage


def provider() -> tuple[str, str] | None:
    """OpenAI wins when both are configured; Bedrock stays a working fallback."""
    if os.environ.get("OPENAI_API_KEY"):
        return "openai", os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    if os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        return "bedrock", BEDROCK_MODEL
    return None


def narrate(prompt: str, span_name: str = "llm.narrate") -> tuple[str | None, str]:
    """Returns the text and the provider label, so evidence files can name what produced them."""
    selected = provider()
    if selected is None:
        return None, "none"
    name, model = selected

    if name == "openai":
        url, headers = OPENAI_URL, {
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
        payload = {"model": model, "input": prompt, "reasoning": {"effort": "low"}}
        label = f"{model} via OpenAI"
    else:
        region = os.environ.get("AWS_REGION", "ap-south-1")
        url = f"https://bedrock-mantle.{region}.api.aws/v1/responses"
        headers = {"Authorization": f"Bearer {os.environ['AWS_BEARER_TOKEN_BEDROCK']}"}
        payload = {"model": model, "input": [{"role": "user", "content": prompt}]}
        label = f"{model} via Bedrock"

    with otel.generation(span_name, model, prompt) as record:
        body = post(url, payload, headers)
        if body is None:
            return None, label
        text, usage = read_responses_api(body)
        otel.completed(record, text or "", usage)
    return text, label
