"""One optional LLM call, traced to Langfuse. Google when a key is set, else OpenAI,
else Bedrock, else a no-op that changes no output (D33).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import otel

GOOGLE_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent?key={key}")
DEFAULT_GOOGLE_MODEL = "gemini-3-flash-preview"
OPENAI_URL = "https://api.openai.com/v1/responses"
DEFAULT_OPENAI_MODEL = "gpt-5.2"
BEDROCK_MODEL = "openai.gpt-oss-120b"
VENDORS = {"google": "Google AI Studio", "openai": "OpenAI", "bedrock": "Bedrock"}


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
    """OpenAI and Bedrock both speak the Responses shape, so one reader serves both."""
    usage = body.get("usage") or {}
    for item in body.get("output", []):
        if item.get("type") == "message":
            for chunk in item.get("content", []) or []:
                if chunk.get("type") == "output_text":
                    return chunk["text"].strip(), usage
    return None, usage


def read_google(body: dict) -> tuple[str | None, dict]:
    """A part may carry a thought signature and no text, so read text across every part."""
    metadata = body.get("usageMetadata") or {}
    usage = {"input_tokens": metadata.get("promptTokenCount"),
             "output_tokens": metadata.get("candidatesTokenCount")}
    candidates = body.get("candidates") or [{}]
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p["text"] for p in parts if isinstance(p.get("text"), str)).strip()
    return text or None, usage


def providers() -> list[tuple[str, str]]:
    """Google first, then OpenAI, then Bedrock, each listed only when its key is set."""
    found = []
    if os.environ.get("GOOGLE_KEY"):
        found.append(("google", os.environ.get("GOOGLE_MODEL") or DEFAULT_GOOGLE_MODEL))
    if os.environ.get("OPENAI_API_KEY"):
        found.append(("openai", os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL))
    if os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        found.append(("bedrock", BEDROCK_MODEL))
    return found


def request(name: str, model: str, prompt: str) -> tuple[str, dict, dict]:
    if name == "google":
        return (GOOGLE_URL.format(model=model, key=os.environ["GOOGLE_KEY"]), {},
                {"contents": [{"parts": [{"text": prompt}]}]})
    if name == "openai":
        return (OPENAI_URL, {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                {"model": model, "input": prompt, "reasoning": {"effort": "low"}})
    region = os.environ.get("AWS_REGION", "ap-south-1")
    return (f"https://bedrock-mantle.{region}.api.aws/v1/responses",
            {"Authorization": f"Bearer {os.environ['AWS_BEARER_TOKEN_BEDROCK']}"},
            {"model": model, "input": [{"role": "user", "content": prompt}]})


def narrate(prompt: str, span_name: str = "llm.narrate") -> tuple[str | None, str]:
    """Returns the text and the provider label, so evidence files can name what produced them.
    A dead provider falls through to the next; all of them dead reads as no key at all."""
    for name, model in providers():
        url, headers, payload = request(name, model, prompt)
        text = None
        with otel.generation(span_name, model, prompt) as record:
            otel.note(record, **{"gen_ai.system": name})
            body = post(url, payload, headers)
            if body is not None:
                reader = read_google if name == "google" else read_responses_api
                text, usage = reader(body)
                otel.completed(record, text or "", usage)
        if text:
            return text, f"{model} via {VENDORS[name]}"
    return None, "none"
