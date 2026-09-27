# -*- coding: utf-8 -*-
"""6b 取证探针①：对本地 Ollama 的 4 个模型各做一次最小调用，量**时延**并确认 OpenAI 兼容面可用。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:11434/v1/chat/completions"
MODELS = [
    "gemma4:latest",
    "gemma4:12b",
    "qwen3.8:27b",
    "SimonPu/Qwen3-Coder:30B-Instruct_Q4_K_XL",
]

for model in MODELS:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "只回答两个字：收到"}],
            "temperature": 0,
            "max_tokens": 16,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        BASE, data=body, headers={"Content-Type": "application/json"}
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            payload = json.loads(response.read().decode("utf-8"))
        text = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage") or {}
        print(f"{model}: OK {time.time() - started:.1f}s text={text!r} usage={usage}")
    except Exception as error:  # noqa: BLE001
        print(f"{model}: FAIL {time.time() - started:.1f}s {type(error).__name__}: {error}")
