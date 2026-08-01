"""One optional LLM call, for the problem statement's own "LLM & ClickStack" decline-
alert use case. A no-op unless AWS_BEARER_TOKEN_BEDROCK is set, same off-by-default
pattern as otel.py. Claude via Bedrock is not reachable in this account (D30: the
cross-region inference profile's token quota is stuck at 0 and not self-service
adjustable); openai.gpt-oss-120b through Bedrock's OpenAI-compatible endpoint is,
verified with a real call. One call, not a chain, per D11's own restraint.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

MODEL = "openai.gpt-oss-120b"


def narrate(prompt: str) -> str | None:
    token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    if not token:
        return None
    region = os.environ.get("AWS_REGION", "ap-south-1")
    url = f"https://bedrock-mantle.{region}.api.aws/v1/responses"
    payload = json.dumps({
        "model": MODEL,
        "input": [{"role": "user", "content": prompt}],
    }).encode()
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"bedrock: narration skipped, {exc}")
        return None
    for item in body.get("output", []):
        if item.get("type") == "message":
            for chunk in item.get("content", []):
                if chunk.get("type") == "output_text":
                    return chunk["text"].strip()
    return None
