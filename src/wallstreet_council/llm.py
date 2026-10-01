"""One chat() call for every council seat, whatever the provider.

Providers:
  nvidia/<model>   NVIDIA NIM, OpenAI-compatible, key from env NVIDIA_API_KEY or keychain `nvidia-api-key`
  gemini/<model>   Google AI Studio, key from env GEMINI_API_KEY or keychain `gemini-api-key`
  codex/<model>    the local Codex CLI signed in through ChatGPT (no API key, OPENAI_API_KEY is stripped)
  claude/<model>   the local Claude Code CLI signed in through claude.ai (ANTHROPIC_API_KEY is stripped)
  copilot/<model>  the local GitHub Copilot CLI signed in through GitHub (tools denied, low reasoning effort)

Keys are never written to disk or logged.

Subscription seats (Codex on the ChatGPT plan, Claude on the Max plan) are rationed per US Eastern day:
  COUNCIL_CODEX_DAILY   default 2 calls   (one chair ruling per full council, two councils a day)
  COUNCIL_CLAUDE_DAILY  default 2 calls   (Claude sits only when a council explicitly seats it)
  COUNCIL_COPILOT_DAILY default 2 calls   (GitHub Copilot CLI, signed in through GitHub; judges Indian councils)
When the ration is spent, chat() raises BudgetSpent and the seat's fallback model takes over.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

CODEX_CANDIDATES = [
    os.environ.get("CODEX_BIN", ""),
    shutil.which("codex") or "",
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
    "/Applications/Codex.app/Contents/Resources/codex",
]
CLAUDE_CANDIDATES = [os.environ.get("CLAUDE_BIN", ""), shutil.which("claude") or "",
                     str(Path.home() / ".local/bin/claude")]


class LLMError(RuntimeError):
    pass


class BudgetSpent(LLMError):
    pass


def daily_budget(provider: str) -> int:
    default = {"codex": "2", "claude": "2", "copilot": "2"}.get(provider)
    if default is None:
        return 10**9
    return int(os.environ.get(f"COUNCIL_{provider.upper()}_DAILY", default))


def budget_left(provider: str) -> int:
    from . import store
    return max(0, daily_budget(provider) - store.usage_today(provider))


@lru_cache(maxsize=None)
def _secret(env: str, service: str) -> str:
    v = os.environ.get(env, "").strip()
    if v:
        return v
    try:
        return subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def _bin(cands: list[str]) -> str:
    for c in cands:
        if c and Path(c).exists() and os.access(c, os.X_OK):
            return c
    return ""


def _post(url: str, body: dict, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise LLMError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
    except (TimeoutError, urllib.error.URLError, OSError) as e:
        raise LLMError(f"{type(e).__name__}: {e}"[:300]) from None


def _strip_reasoning(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def _nvidia(model: str, system: str, user: str, max_tokens: int, temperature: float, timeout: int) -> str:
    key = _secret("NVIDIA_API_KEY", "nvidia-api-key")
    if not key:
        raise LLMError("no NVIDIA key (env NVIDIA_API_KEY or keychain nvidia-api-key)")
    body = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            **_nvidia_extras(model)}
    d = _post(f"{NVIDIA_BASE}/chat/completions", body, {"Authorization": f"Bearer {key}"}, timeout)
    choice = d["choices"][0]
    text = _strip_reasoning(choice["message"].get("content") or "")
    if not text and choice.get("finish_reason") == "length":
        # hidden reasoning ate the whole budget on a long brief; one retry with room to answer
        body["max_tokens"] = max_tokens * 3
        d = _post(f"{NVIDIA_BASE}/chat/completions", body, {"Authorization": f"Bearer {key}"}, timeout)
        text = _strip_reasoning(d["choices"][0]["message"].get("content") or "")
    return text


def _nvidia_extras(model: str) -> dict:
    """Keep reasoning models from spending the answer budget on thinking. Measured 2026-09-30:
    long briefs made gpt-oss, glm-5.3 and nemotron-3-super return empty content."""
    if model.startswith("openai/gpt-oss"):
        return {"reasoning_effort": "low"}
    if model.startswith(("nvidia/nemotron-3", "z-ai/glm")):
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {}


def _gemini(model: str, system: str, user: str, max_tokens: int, temperature: float, timeout: int) -> str:
    key = _secret("GEMINI_API_KEY", "gemini-api-key")
    if not key:
        raise LLMError("no Gemini key (env GEMINI_API_KEY or keychain gemini-api-key)")
    d = _post(f"{GEMINI_BASE}/models/{model}:generateContent", {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
    }, {"x-goog-api-key": key}, timeout)
    cand = (d.get("candidates") or [{}])[0]
    return "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))


def _subscription_env(drop: str) -> dict:
    env = dict(os.environ)
    env.pop(drop, None)  # never fall back to API billing
    return env


def _codex(model: str, system: str, user: str, timeout: int) -> str:
    exe = _bin(CODEX_CANDIDATES)
    if not exe:
        raise LLMError("Codex CLI not found (set CODEX_BIN)")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "last.txt"
        # low reasoning effort and no tools: the cheapest way to spend a ChatGPT-plan turn
        cmd = [exe, "exec", "--skip-git-repo-check", "-s", "read-only", "-C", tmp, "-o", str(out),
               "-c", f'model_reasoning_effort="{os.environ.get("COUNCIL_CODEX_EFFORT", "low")}"']
        if model and model != "default":
            cmd += ["-m", model]
        cmd.append(f"{system}\n\n{user}\n\nAnswer directly. Do not run any commands.")
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           env=_subscription_env("OPENAI_API_KEY"))
        if out.exists() and out.read_text().strip():
            return out.read_text()
        raise LLMError(f"codex exit {p.returncode}: {(p.stderr or p.stdout)[-300:]}")


COPILOT_CANDIDATES = [os.environ.get("COPILOT_BIN", ""), shutil.which("copilot") or "", "/opt/homebrew/bin/copilot"]


def _copilot(model: str, system: str, user: str, timeout: int) -> str:
    exe = _bin(COPILOT_CANDIDATES)
    if not exe:
        raise LLMError("GitHub Copilot CLI not found (npm install -g @github/copilot)")
    cmd = [exe, "-p", f"{system}\n\n{user}\n\nAnswer directly. Do not use any tools.", "-s", "--no-color",
           "--deny-tool", "shell", "--deny-tool", "write"]
    if model and model not in ("default", "auto"):  # Copilot's automatic model choice rejects an effort setting
        cmd += ["--model", model, "--reasoning-effort", os.environ.get("COUNCIL_COPILOT_EFFORT", "low")]
    with tempfile.TemporaryDirectory() as tmp:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=tmp)
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout
    raise LLMError(f"copilot exit {p.returncode}: {(p.stderr or p.stdout)[-300:]}")


def _claude(model: str, system: str, user: str, timeout: int) -> str:
    exe = _bin(CLAUDE_CANDIDATES)
    if not exe:
        raise LLMError("Claude Code CLI not found (set CLAUDE_BIN)")
    cmd = [exe, "-p", "--model", model or "opus", "--append-system-prompt", system,
           "--disallowedTools", "Bash,Edit,Write,WebFetch,WebSearch", user]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                       env=_subscription_env("ANTHROPIC_API_KEY"))
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout
    raise LLMError(f"claude exit {p.returncode}: {(p.stderr or p.stdout)[-300:]}")


def chat(model: str, system: str, user: str, *, max_tokens: int = 1800, temperature: float = 0.4,
         timeout: int = 150, allow_over_budget: bool = False) -> dict:
    """Returns {"text", "model", "ms"}. Raises LLMError (BudgetSpent for a rationed seat)."""
    provider, _, name = model.partition("/")
    t0 = time.time()
    if provider in ("nvidia", "gemini"):
        fn = _nvidia if provider == "nvidia" else _gemini
        for attempt in range(3):  # busy models answer 429/503; back off and retry
            try:
                text = fn(name, system, user, max_tokens, temperature, timeout)
                break
            except LLMError as e:
                if attempt == 2 or not re.search(r"HTTP (429|500|502|503|504)", str(e)):
                    raise
                time.sleep(4 * (attempt + 1))
    elif provider in ("codex", "claude", "copilot"):
        from . import store
        if budget_left(provider) <= 0 and not allow_over_budget:
            raise BudgetSpent(f"{provider} daily ration of {daily_budget(provider)} calls is spent")
        try:
            text = {"codex": _codex, "claude": _claude, "copilot": _copilot}[provider](name, system, user, timeout)
        except Exception:
            store.record_usage(provider, model, False)
            raise
        store.record_usage(provider, model, True)
    else:
        raise LLMError(f"unknown provider in {model!r}")
    if not text.strip():
        raise LLMError("empty reply")
    return {"text": text.strip(), "model": model, "ms": int((time.time() - t0) * 1000)}


def parse_json(text: str):
    """Pull the first JSON object or array out of a model reply."""
    text = _strip_reasoning(text)
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start < 0:
            continue
        depth, in_str, esc = 0, False, False
        for i, ch in enumerate(text[start:], start):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    return None
