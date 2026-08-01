"""One optional LLM call for decline alerting. No-op unless AWS_BEARER_TOKEN_BEDROCK
is set; see D30 for why openai.gpt-oss-120b, not Claude.
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
