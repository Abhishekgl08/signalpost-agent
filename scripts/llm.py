"""Groq multi-key rotator (adapted from exam-intelligence-ingestion/groq_key_rotator.py).

Keys are read ONLY from env var GROQ_KEYS (comma separated) or a local .env file.
No keys are hardcoded, so this repo is safe to publish.
"""
import os
import threading
import time
from pathlib import Path

DEFAULT_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")


def load_keys():
    raw = os.getenv("GROQ_KEYS") or os.getenv("GROQ_API_KEY")
    if not raw:
        for p in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
            if p.exists():
                for line in p.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("GROQ_KEYS=") or line.startswith("GROQ_API_KEY="):
                        raw = line.split("=", 1)[1]
                        break
            if raw:
                break
    keys = [k.strip() for k in (raw or "").split(",") if k.strip()]
    if not keys:
        raise RuntimeError("No GROQ_KEYS or GROQ_API_KEY found (env var or .env)")
    return keys


class Rotator:
    def __init__(self, keys=None):
        self.keys = keys or load_keys()
        self.i = 0
        self.cool = {}
        self.lock = threading.Lock()

    def _next(self):
        from groq import Groq
        with self.lock:
            for _ in range(len(self.keys)):
                idx = self.i
                self.i = (self.i + 1) % len(self.keys)
                if time.time() >= self.cool.get(idx, 0):
                    return Groq(api_key=self.keys[idx], max_retries=0), idx
            wait = max(0.5, min(self.cool.values()) - time.time())
        time.sleep(wait)
        return self._next()

    def cooldown(self, idx, secs):
        with self.lock:
            self.cool[idx] = time.time() + secs


_R = None


def chat(messages, model=None, temperature=0.0, max_tokens=800, json_mode=False, tries=None):
    global _R
    _R = _R or Rotator()
    tries = tries or len(_R.keys) * 2
    err = None
    for _ in range(tries):
        client, idx = _R._next()
        try:
            kw = {"response_format": {"type": "json_object"}} if json_mode else {}
            r = client.chat.completions.create(
                model=model or DEFAULT_MODEL, messages=messages,
                temperature=temperature, max_tokens=max_tokens, **kw)
            return r.choices[0].message.content.strip()
        except Exception as e:
            err = e
            m = str(e).lower()
            _R.cooldown(idx, 10 if ("429" in m or "rate" in m) else 3)
            time.sleep(0.3)
    raise RuntimeError(f"all Groq keys failed: {err}")
