"""MCP server. Lets Claude Code, Codex or any MCP client convene the council and read along.

Registers as a local STDIO server. On start it also brings up the live monitor at
http://127.0.0.1:8765 so a browser can watch every council this server runs.

There is no tool that places an order. Positions are paper only.
"""
from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import council as council_mod
from . import monitor, scoreboard, store

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

mcp = MCPServer(
    "wallstreet-council",
    instructions=(
        "A panel of AI models (Gemini, NVIDIA-hosted models, Codex, optionally Claude) that debate which US stock "
        "to buy with a small budget, as a PAPER-TRADING simulation. convene_council starts a debate in the "
        "background and returns a session id and a live monitor URL; poll get_transcript with `since` to follow "
        "it. Nothing is ever bought. Present rulings as model opinions, never as financial advice."
    ),
)
MONITOR_URL = ""


def _monitor() -> str:
    global MONITOR_URL
    if not MONITOR_URL:
        MONITOR_URL = monitor.ensure_background()
    return MONITOR_URL


@mcp.tool(annotations=WRITE, structured_output=True)
def convene_council(budget: float = 10.0, horizon: str = "end of this month", tickers: list[str] | None = None,
                    rounds: int = 1, include_claude: bool = False) -> dict[str, Any]:
    """Start a council debate in the background. Returns the session id and the live monitor URL.

    budget: paper dollars to allocate. horizon: plain-English horizon. tickers: extra symbols to consider on top
    of today's screened movers and the core watchlist. rounds: cross-examination rounds (0 to 3).
    include_claude: seat Claude Opus through the local Claude Code CLI (uses the Max plan).
    A full council takes about 3 to 8 minutes.
    """
    url = _monitor()
    sid = monitor.start_council_thread(budget=budget, horizon=horizon, tickers=tickers or [],
                                       rounds=max(0, min(int(rounds), 3)), include_claude=include_claude)
    return {"session": sid, "monitor": f"{url}/#{sid}", "status": "running"}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def get_transcript(session: str, since: int = 0, limit: int = 200) -> dict[str, Any]:
    """What the council has said in a session after event number `since`. Pass the returned `next_since` back
    to follow a live debate."""
    evs = store.events(session, since, limit)
    s = store.session(session) or {}
    return {"status": s.get("status"), "verdict": s.get("verdict"), "error": s.get("error"),
            "events": [{k: e[k] for k in ("seq", "phase", "speaker", "model", "kind", "text")} for e in evs],
            "next_since": evs[-1]["seq"] if evs else since}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def list_sessions(limit: int = 10) -> dict[str, Any]:
    """Recent councils with their status and ruling."""
    return {"sessions": [{"id": s["id"], "status": s["status"], "budget": s["budget"], "horizon": s["horizon"],
                          "decision": (s["verdict"] or {}).get("decision"),
                          "entry_window_et": (s["verdict"] or {}).get("entry_window_et")}
                         for s in store.sessions(limit)]}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def get_ruling(session: str) -> dict[str, Any]:
    """The chair's full ruling for a finished session, including the volatility band for the budget."""
    s = store.session(session)
    if not s:
        return {"error": "no such session"}
    return {"status": s["status"], "ruling": s["verdict"], "error": s["error"]}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def paper_portfolio() -> dict[str, Any]:
    """Every paper position marked to the latest price."""
    return scoreboard.portfolio()


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def seat_leaderboard() -> dict[str, Any]:
    """Which council seat's final votes have performed best since they were cast."""
    return {"seats": scoreboard.leaderboard()}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def council_seats(include_claude: bool = False) -> dict[str, Any]:
    """The seats, their models and their roles. Edit ~/.wallstreet-council/seats.json to change them."""
    return {"seats": council_mod.load_seats(include_claude), "seats_file": str(council_mod.SEATS_FILE)}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def monitor_url() -> dict[str, Any]:
    """URL of the live monitor page."""
    return {"url": _monitor()}


@mcp.tool(annotations=WRITE, structured_output=True)
def start_live(interval_open: int = 15, interval_closed: int = 60, council_times: str = "09:05,15:30",
               budget: float = 10.0) -> dict[str, Any]:
    """Start the continuous trading floor in its own process: free-tier seats chat every `interval_open`
    minutes while the market is open (`interval_closed` otherwise), and full councils run at `council_times`
    (Eastern, weekdays). Codex chairs only those councils, within its daily ration. Keeps running after this
    MCP server exits; stop it with stop_live."""
    from . import floor
    pid = floor.start_detached(interval_open=interval_open, interval_closed=interval_closed,
                               council_times=council_times, budget=budget)
    return {"pid": pid, "floor_session": floor.floor_id(), "monitor": _monitor()}


@mcp.tool(annotations=WRITE, structured_output=True)
def stop_live() -> dict[str, Any]:
    """Stop the continuous trading floor."""
    from . import floor
    return {"stopped": floor.stop_detached()}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def live_status() -> dict[str, Any]:
    """Whether the floor is running, today's floor session id, and the subscription rations left today."""
    from . import floor, llm
    return {"pid": floor.running_pid(), "floor_session": floor.floor_id(),
            "rations": {p: {"used": store.usage_today(p), "left": llm.budget_left(p), "per_day": llm.daily_budget(p)}
                        for p in ("codex", "claude")}}


def main() -> None:
    _monitor()
    mcp.run()


if __name__ == "__main__":
    main()
