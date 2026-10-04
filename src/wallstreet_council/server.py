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
        "A panel of AI models (Gemini, NVIDIA-hosted models, Codex, optionally Claude) that debate US and Indian "
        "stocks, IPOs and any question the client brings, as a PAPER-TRADING simulation that counts broker charges "
        "and taxes and learns from graded outcomes. convene_council starts a debate in the background and returns "
        "a session id and a live monitor URL; poll get_transcript with `since` to follow it. say_to_floor talks to "
        "the continuous floor. Nothing is ever bought. Present rulings as model opinions, never financial advice."
    ),
)
MONITOR_URL = ""


def _monitor() -> str:
    global MONITOR_URL
    if not MONITOR_URL:
        MONITOR_URL = monitor.ensure_background()
    return MONITOR_URL


@mcp.tool(annotations=WRITE, structured_output=True)
def convene_council(mode: str = "pick", market: str = "US", topic: str | None = None, budget: float | None = None,
                    horizon: str = "end of this month", tickers: list[str] | None = None, rounds: int = 1,
                    include_claude: bool = False, target: float | None = None) -> dict[str, Any]:
    """Start a council debate in the background. Returns the session id and the live monitor URL.

    mode: "pick" (which stock to buy), "ipo" (which IPOs to apply for, avoid or buy after listing), or "topic"
    (any question in `topic`). market: "US", "IN" (NSE/BSE), or "BOTH" (ipo and topic only).
    budget: paper money, default $100 (US) or Rs 10,000 (IN). target: goal amount, default 10x the budget.
    tickers: extra symbols (NSE symbols end in .NS). rounds: cross-examination rounds (0 to 3).
    include_claude: seat Claude Opus through the local Claude Code CLI (uses the Max plan).
    A full council takes about 3 to 8 minutes.
    """
    url = _monitor()
    sid = monitor.start_council_thread(budget=budget, horizon=horizon, tickers=tickers or [],
                                       rounds=max(0, min(int(rounds), 3)), include_claude=include_claude,
                                       mode=mode, market_code=market, topic=topic, target=target)
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
def say_to_floor(text: str) -> dict[str, Any]:
    """Post the client's message or suggestion to the live trading floor ("what if I apply for this IPO and get
    allotted?"). The floor wakes and the next speakers answer it; councils also read recent client messages."""
    from . import floor
    seq = floor.say_to_floor(text)
    return {"posted": seq, "floor_session": floor.floor_id(), "floor_running": bool(floor.running_pid())}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def track_record() -> dict[str, Any]:
    """Every ruling graded against the index (S&P 500 or Nifty 50), per-seat records and trust weights, and the
    lessons the council has learned from its outcomes."""
    from . import learning
    learning.grade()
    return learning.track_record()


@mcp.tool(annotations=WRITE, structured_output=True)
def start_live(interval_open: int = 15, interval_closed: int = 60,
               schedule: str = "pick-US@09:05, pick-IN@09:20, ipo-IN@12:30/Mon", budget: float = 100.0,
               goal: float = 1000.0, publish: bool = False) -> dict[str, Any]:
    """Start the continuous trading floor in its own process: free-tier seats chat every `interval_open`
    minutes while either market is open (`interval_closed` otherwise) about both markets, IPOs, a rotating
    spotlight stock and the client's goal; full councils run on `schedule` (each time in that market's zone,
    weekdays). Codex chairs only those councils, within its daily ration. publish pushes a read-only snapshot to
    GitHub Pages every 15 minutes. Keeps running after this MCP server exits; stop it with stop_live."""
    from . import floor
    pid = floor.start_detached(interval_open=interval_open, interval_closed=interval_closed, schedule=schedule,
                               budget=budget, goal=goal, publish=publish)
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
                        for p in ("codex", "claude", "copilot")}}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def politician_trades(days: int = 45) -> dict[str, Any]:
    """US House members' disclosed stock trades: the most-traded tickers in the last `days`, the latest buys, and the
    honest record of copying them from the day after disclosure against the S&P 500."""
    from . import congress
    sc = congress.scorecard()
    return {"hot_tickers": congress.hot_tickers(days), "copying_record": sc.get("overall"),
            "best_filers_to_copy": sc.get("filers", [])[:10], "recent_buys": sc.get("recent", [])[:20]}


def main() -> None:
    _monitor()
    mcp.run()


if __name__ == "__main__":
    main()
