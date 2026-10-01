"""Live monitor: a local web page that streams the council's conversation as it happens.

    uv run council monitor            -> http://127.0.0.1:8765

Binds to localhost only. Server-sent events poll the SQLite store, so the monitor also shows
councils started from the MCP server or the CLI in another process.
"""
from __future__ import annotations

import asyncio
import json
import threading
from importlib import resources

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from . import scoreboard, store

HOST, PORT = "127.0.0.1", 8765


def start_council_thread(**kw) -> str:
    from .council import Council
    c = Council(**kw)
    threading.Thread(target=lambda: _safe(c.run), daemon=True, name=f"council-{c.id}").start()
    return c.id


def _safe(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001  the failure is already in the event log
        pass


async def index(_: Request):
    return HTMLResponse(resources.files("wallstreet_council").joinpath("monitor.html").read_text())


async def api_sessions(_: Request):
    return JSONResponse(store.sessions(50))


async def api_events(req: Request):
    return JSONResponse(store.events(req.query_params.get("session"), int(req.query_params.get("since", 0))))


async def api_portfolio(_: Request):
    return JSONResponse(await asyncio.to_thread(scoreboard.portfolio))


async def api_leaderboard(_: Request):
    from . import learning
    stats = await asyncio.to_thread(learning.seat_stats)
    return JSONResponse(sorted(stats.values(), key=lambda s: -(s["live_avg_excess_pct"] or -1e9)))


async def api_convene(req: Request):
    b = await req.json()
    try:
        sid = start_council_thread(
            budget=float(b["budget"]) if b.get("budget") else None, horizon=b.get("horizon") or "end of this month",
            tickers=[t.strip().upper() for t in (b.get("tickers") or "").split(",") if t.strip()],
            rounds=int(b.get("rounds", 1)), include_claude=bool(b.get("include_claude")),
            mode=b.get("mode") or "pick", market_code=b.get("market") or "US", topic=b.get("topic") or None)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"session": sid})


async def api_say(req: Request):
    b = await req.json()
    text = (b.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "empty"}, status_code=400)
    sid = b.get("session")
    if sid and not sid.startswith("floor-"):
        store.add_event(sid, "client", "Client", "user", text[:1000])  # a running council reads it next phase
    else:
        from .floor import say_to_floor
        say_to_floor(text)
    return JSONResponse({"ok": True})


async def api_phone(req: Request):
    """Local monitor only (the phone gateway drops this route): the phone link and password."""
    host = req.headers.get("host", "").split(":")[0]
    if host not in ("127.0.0.1", "localhost") or (req.client and req.client.host not in ("127.0.0.1", "::1")):
        return JSONResponse({"error": "local only"}, status_code=403)
    from . import phone
    from .service import AGENTS
    return JSONResponse({"url": phone.current_url(), "installed": (AGENTS / "com.wallstreet-council.tunnel.plist").exists(),
                         "password": phone.password() if req.query_params.get("reveal") == "1" else None})


async def api_track(_: Request):
    from . import learning
    return JSONResponse(await asyncio.to_thread(learning.track_record))


async def api_budget(_: Request):
    from . import floor, llm
    return JSONResponse({"live_pid": floor.running_pid(), "floor_session": floor.floor_id(),
                         **{p: {"used": store.usage_today(p), "left": llm.budget_left(p),
                                "per_day": llm.daily_budget(p)} for p in ("codex", "claude", "copilot")}})


async def api_stream(req: Request):
    sid = req.query_params.get("session")
    since = int(req.query_params.get("since", 0))

    async def gen():
        nonlocal since
        while not await req.is_disconnected():
            for e in store.events(sid, since):
                since = e["seq"]
                yield f"data: {json.dumps(e, default=str)}\n\n"
            yield ": keepalive\n\n"
            await asyncio.sleep(0.7)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


app = Starlette(routes=[
    Route("/", index), Route("/api/sessions", api_sessions), Route("/api/events", api_events),
    Route("/api/stream", api_stream), Route("/api/portfolio", api_portfolio),
    Route("/api/leaderboard", api_leaderboard), Route("/api/budget", api_budget),
    Route("/api/say", api_say, methods=["POST"]), Route("/api/track", api_track), Route("/api/phone", api_phone), Route("/api/convene", api_convene, methods=["POST"]),
])


def serve(host: str = HOST, port: int = PORT) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")


_bg: threading.Thread | None = None


def ensure_background(port: int = PORT) -> str:
    """Start the monitor in a daemon thread unless something already listens on the port."""
    global _bg
    import socket
    with socket.socket() as s:
        if s.connect_ex((HOST, port)) == 0:
            return f"http://{HOST}:{port}"
    if _bg is None:
        import uvicorn
        server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning"))
        _bg = threading.Thread(target=server.run, daemon=True, name="council-monitor")
        _bg.start()
    return f"http://{HOST}:{port}"
