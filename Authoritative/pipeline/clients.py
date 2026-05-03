"""Multi-provider LLM client wrapper.

Round-robins across keys per provider so each pipeline stage hits a
different rate-limit pool. Per-stage routing config:

  source_select / extract  -> openai_pool (gpt-4o)
  build                    -> openai_pool (gpt-4o)
  verify                   -> anthropic_pool (claude-haiku-4-5)

If anthropic is unavailable, verify falls back to gemini.
"""
from __future__ import annotations
import itertools
import time
import threading
from typing import Optional

from . import config

# OpenAI
try:
    from openai import OpenAI
    _OA_CLIENTS = [OpenAI(api_key=k) for k in config.OPENAI_KEYS]
except Exception:
    _OA_CLIENTS = []

# Anthropic
try:
    from anthropic import Anthropic
    _AN_CLIENTS = [Anthropic(api_key=k) for k in config.ANTHROPIC_KEYS]
except Exception:
    _AN_CLIENTS = []

# Together (OpenAI-compatible API)
try:
    from openai import OpenAI as _OAI
    _TG_CLIENTS = [_OAI(api_key=k, base_url="https://api.together.xyz/v1")
                   for k in config.TOGETHER_KEYS]
except Exception:
    _TG_CLIENTS = []

# Gemini (genai)
try:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        import google.generativeai as genai
    _GEMINI_KEYS = list(config.GEMINI_KEYS)
except Exception:
    genai = None
    _GEMINI_KEYS = []


class _RoundRobin:
    def __init__(self, items):
        self._items = list(items)
        self._idx = 0
        self._lock = threading.Lock()

    def next(self):
        if not self._items:
            return None
        with self._lock:
            item = self._items[self._idx % len(self._items)]
            self._idx += 1
            return item


_oa_rr = _RoundRobin(_OA_CLIENTS)
_an_rr = _RoundRobin(_AN_CLIENTS)
_tg_rr = _RoundRobin(_TG_CLIENTS)
_gemini_rr = _RoundRobin(_GEMINI_KEYS)


def call_openai(prompt: str, *, model: str = None, temperature: float = 0.0,
                max_tokens: int = 1200, response_format: Optional[dict] = None,
                system: Optional[str] = None, retries: int = 3) -> tuple[str, Optional[str]]:
    """Returns (text, error). text='' if error."""
    model = model or config.BUILD_MODEL
    client = _oa_rr.next()
    if client is None:
        return "", "no_openai_client"
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    kw = dict(model=model, temperature=temperature, max_tokens=max_tokens, messages=msgs)
    if response_format:
        kw["response_format"] = response_format
    last_err = None
    for attempt in range(retries):
        try:
            r = client.chat.completions.create(**kw)
            return (r.choices[0].message.content or "").strip(), None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (attempt + 1))
            client = _oa_rr.next() or client  # rotate key on retry
    return "", last_err


def call_anthropic(prompt: str, *, model: str = "claude-haiku-4-5",
                   temperature: float = 0.0, max_tokens: int = 1200,
                   system: Optional[str] = None, retries: int = 3) -> tuple[str, Optional[str]]:
    """Returns (text, error)."""
    client = _an_rr.next()
    if client is None:
        return "", "no_anthropic_client"
    last_err = None
    for attempt in range(retries):
        try:
            kw = dict(model=model, temperature=temperature, max_tokens=max_tokens,
                      messages=[{"role": "user", "content": prompt}])
            if system:
                kw["system"] = system
            r = client.messages.create(**kw)
            # Concatenate any text blocks
            chunks = []
            for blk in r.content:
                t = getattr(blk, "text", None)
                if t:
                    chunks.append(t)
            return "".join(chunks).strip(), None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (attempt + 1))
    return "", last_err


def call_gemini(prompt: str, *, model: str = "gemini-2.5-flash",
                temperature: float = 0.0, max_tokens: int = 1200,
                system: Optional[str] = None, retries: int = 3) -> tuple[str, Optional[str]]:
    """Returns (text, error)."""
    if genai is None:
        return "", "no_gemini_module"
    key = _gemini_rr.next()
    if key is None:
        return "", "no_gemini_key"
    last_err = None
    for attempt in range(retries):
        try:
            genai.configure(api_key=key)
            kwargs = {}
            if system:
                kwargs["system_instruction"] = system
            m = genai.GenerativeModel(model, **kwargs)
            r = m.generate_content(
                prompt,
                generation_config={"temperature": temperature, "max_output_tokens": max_tokens},
            )
            return (r.text or "").strip(), None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (attempt + 1))
            key = _gemini_rr.next() or key
    return "", last_err


def call_together(prompt: str, *, model: str = "Qwen/Qwen3-235B-A22B-Instruct-2507-tput",
                  temperature: float = 0.0, max_tokens: int = 1200,
                  system: Optional[str] = None, retries: int = 3) -> tuple[str, Optional[str]]:
    """Returns (text, error). Uses OpenAI-compatible Together endpoint."""
    client = _tg_rr.next()
    if client is None:
        return "", "no_together_client"
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    last_err = None
    for attempt in range(retries):
        try:
            r = client.chat.completions.create(
                model=model, temperature=temperature,
                max_tokens=max_tokens, messages=msgs,
            )
            return (r.choices[0].message.content or "").strip(), None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (attempt + 1))
            client = _tg_rr.next() or client
    return "", last_err


def call_verify(prompt: str, *, max_tokens: int = 600, system: Optional[str] = None,
                response_format_json: bool = True) -> tuple[str, Optional[str]]:
    """Verification calls — try Anthropic Haiku first, fall back to Together
    Llama 3.3 70B. Both are independent rate pools from the OpenAI gpt-4o
    used by the generator."""
    if _AN_CLIENTS:
        txt, err = call_anthropic(prompt, model=config.VERIFICATION_MODEL,
                                  temperature=0.0, max_tokens=max_tokens, system=system)
        if not err and txt:
            return txt, None
    if _TG_CLIENTS:
        txt, err = call_together(prompt, temperature=0.0, max_tokens=max_tokens, system=system)
        if not err and txt:
            return txt, None
    # last-resort fallback: OpenAI mini
    rf = {"type": "json_object"} if response_format_json else None
    return call_openai(prompt, model="gpt-4o-mini", temperature=0.0,
                       max_tokens=max_tokens, response_format=rf, system=system)


def provider_status() -> dict:
    return {
        "openai_keys": len(_OA_CLIENTS),
        "anthropic_keys": len(_AN_CLIENTS),
        "together_keys": len(_TG_CLIENTS),
        "gemini_keys": len(_GEMINI_KEYS),
    }
