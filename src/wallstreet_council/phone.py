"""Password-protected access from a phone.

A second copy of the monitor listens on 127.0.0.1:8766 and requires a login on every request. A Cloudflare
quick tunnel (`cloudflared`, no account needed) gives it a public https address. The password-free monitor on
:8765 is never exposed.

  password   generated once, kept in the macOS keychain (service `wallstreet-council-phone`); the local monitor's
             "Phone access" panel reveals it and shows a QR code for the link. `council phone --new-password`
             rotates it and signs every phone out.
  sessions   an HMAC-signed cookie valid for 30 days (secret in keychain `wallstreet-council-secret`)
  limits     5 failed logins per address per 10 minutes, 30 overall, then refusals until the window passes
  address    quick-tunnel URLs change when cloudflared restarts (for example after a reboot); the current one is
             in ~/.wallstreet-council/phone_url.txt and on the local monitor
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import subprocess
import time
from collections import deque

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from .floor import HOME

PORT = 8766
PW_SERVICE, SECRET_SERVICE = "wallstreet-council-phone", "wallstreet-council-secret"
URL_FILE = HOME / "phone_url.txt"
TUNNEL_LOG = HOME / "tunnel.log"
COOKIE, MAX_AGE = "wsc_session", 30 * 24 * 3600
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes, easy on a phone keyboard


def _kc_get(service: str) -> str:
    r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _kc_set(service: str, value: str) -> None:
    subprocess.run(["/usr/bin/security", "add-generic-password", "-U", "-a", "wallstreet-council", "-s", service,
                    "-w", value], capture_output=True, check=True)


def password(rotate: bool = False) -> str:
    pw = "" if rotate else _kc_get(PW_SERVICE)
    if not pw:
        pw = "-".join("".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(4))
        _kc_set(PW_SERVICE, pw)
        if rotate:  # new password: invalidate every existing phone session
            _kc_set(SECRET_SERVICE, secrets.token_hex(32))
    return pw


def _secret() -> bytes:
    s = _kc_get(SECRET_SERVICE)
    if not s:
        s = secrets.token_hex(32)
        _kc_set(SECRET_SERVICE, s)
    return s.encode()


def _sign(expiry: int) -> str:
    mac = hmac.new(_secret(), f"ok|{expiry}".encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{mac}"


def _valid(token: str | None) -> bool:
    try:
        expiry_s, mac = (token or "").split(".", 1)
        expiry = int(expiry_s)
    except ValueError:
        return False
    return expiry > time.time() and hmac.compare_digest(_sign(expiry).split(".", 1)[1], mac)


def current_url() -> str | None:
    try:
        return URL_FILE.read_text().strip() or None
    except OSError:
        pass
    try:  # fall back to the tunnel's own log
        found = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", TUNNEL_LOG.read_text()[-200000:])
        return found[-1] if found else None
    except OSError:
        return None


# ---- login ------------------------------------------------------------------------------------------
_fails: dict[str, deque] = {}
_all_fails: deque = deque()
WINDOW = 600

LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Wall Street Council</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0e14;color:#e6e9ef;
font:16px/1.5 system-ui,-apple-system,sans-serif}form{width:min(340px,90vw);display:grid;gap:12px}
h1{font-size:20px;margin:0}h1 span{color:#f5c451}input,button{font:inherit;padding:12px;border-radius:10px;
border:1px solid #242c3d;background:#171d2b;color:#e6e9ef}button{background:#f5c451;color:#1a1400;font-weight:700;
border:0}p{color:#ff6b6b;margin:0;min-height:1.5em}</style></head><body><form method="post" action="login">
<h1>Wall Street <span>Council</span></h1><input type="password" name="password" placeholder="Password"
autocomplete="current-password" autofocus required><button>Enter</button><p>__ERR__</p></form></body></html>"""


def _client_ip(req: Request) -> str:
    return req.headers.get("cf-connecting-ip") or (req.client.host if req.client else "?")


def _limited(ip: str) -> bool:
    now = time.time()
    for q in [_fails.setdefault(ip, deque()), _all_fails]:
        while q and now - q[0] > WINDOW:
            q.popleft()
    return len(_fails[ip]) >= 5 or len(_all_fails) >= 30


async def login_page(_: Request):
    return HTMLResponse(LOGIN_HTML.replace("__ERR__", ""))


async def login_post(req: Request):
    ip = _client_ip(req)
    if _limited(ip):
        return HTMLResponse(LOGIN_HTML.replace("__ERR__", "Too many attempts. Try again in 10 minutes."), 429)
    form = await req.form()
    if hmac.compare_digest(str(form.get("password", "")).strip().encode(), password().encode()):
        resp = RedirectResponse("./", 303)
        resp.set_cookie(COOKIE, _sign(int(time.time()) + MAX_AGE), max_age=MAX_AGE, httponly=True, secure=True,
                        samesite="lax")
        return resp
    now = time.time()
    _fails[ip].append(now)
    _all_fails.append(now)
    return HTMLResponse(LOGIN_HTML.replace("__ERR__", "Wrong password."), 401)


class RequireLogin(BaseHTTPMiddleware):
    async def dispatch(self, req: Request, call_next):
        if req.url.path in ("/login",) or _valid(req.cookies.get(COOKIE)):
            return await call_next(req)
        if req.url.path.startswith("/api/"):
            return JSONResponse({"error": "login required"}, 401)
        return RedirectResponse("login", 303)


def build_app() -> Starlette:
    from . import monitor
    routes = [Route("/login", login_page), Route("/login", login_post, methods=["POST"])]
    routes += [r for r in monitor.app.routes if getattr(r, "path", "") != "/api/phone"]  # never reveal the password
    app = Starlette(routes=routes)
    app.add_middleware(RequireLogin)
    return app


def serve() -> None:
    import uvicorn
    password()  # make sure one exists
    uvicorn.run(build_app(), host="127.0.0.1", port=PORT, log_level="warning", proxy_headers=True)


def run_tunnel() -> None:
    """Run cloudflared in the foreground (launchd keeps it alive) and record the URL it is given."""
    import shutil
    exe = shutil.which("cloudflared") or "/opt/homebrew/bin/cloudflared"
    URL_FILE.unlink(missing_ok=True)
    p = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    with open(TUNNEL_LOG, "a") as log:
        for line in p.stdout:  # type: ignore[union-attr]
            log.write(line)
            log.flush()
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m:
                URL_FILE.write_text(m.group(0))
    raise SystemExit(p.wait())
