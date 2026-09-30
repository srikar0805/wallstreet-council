"""Command line.

    council run [--market US|IN] [--budget 10] [--tickers NVDA] [--rounds 1] [--claude]   stock-pick council
    council ipo [--market IN|US|BOTH] [--topic "..."]                                        IPO council
    council debate "any question" [--market IN|US|BOTH]                                     topic council
    council say "message"                                        talk to the live floor (they answer next round)
    council live [--schedule "pick-US@09:05, pick-IN@09:20, ipo-IN@12:30"] [--publish]      talk continuously
    council track | grade | reflect | publish                     track record, grading, lessons, public site
    council install-service [--publish] | uninstall-service      keep floor + monitor running (macOS launchd)
    council monitor                                                          live web monitor
    council budget                                                           ChatGPT / Claude rations left today
    council mcp                                                              MCP server over STDIO
    council ping                                                             one tiny call per seat
    council portfolio | leaderboard
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time


def _tail(sid: str, stop: threading.Event) -> None:
    from . import store
    since = 0
    while True:
        for e in store.events(sid, since):
            since = e["seq"]
            who = f"{e['speaker']}" + (f" [{e['model']}]" if e["model"] else "")
            print(f"\n[{e['phase']}] {who}: {e['text']}", flush=True)
        if stop.is_set():
            return
        time.sleep(0.5)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="council")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--market", default="US", choices=["US", "IN"])
    r.add_argument("--target", type=float, default=None, help="goal amount (default 10x budget; 0 disables)")
    r.add_argument("--budget", type=float, default=None, help="default $10 (US) or Rs 1000 (IN)")
    r.add_argument("--horizon", default="end of this month")
    r.add_argument("--tickers", default="")
    r.add_argument("--rounds", type=int, default=1)
    r.add_argument("--claude", action="store_true", help="seat Claude Opus via the Claude Code CLI")
    r.add_argument("--no-position", action="store_true", help="do not open a paper position")
    ip = sub.add_parser("ipo")
    ip.add_argument("--market", default="IN", choices=["US", "IN", "BOTH"])
    ip.add_argument("--topic", default=None, help="focus, e.g. 'Should I apply to the Srit India IPO?'")
    ip.add_argument("--budget", type=float, default=None)
    ip.add_argument("--rounds", type=int, default=1)
    db = sub.add_parser("debate")
    db.add_argument("topic")
    db.add_argument("--market", default="BOTH", choices=["US", "IN", "BOTH"])
    db.add_argument("--tickers", default="")
    db.add_argument("--rounds", type=int, default=1)
    sy = sub.add_parser("say")
    sy.add_argument("text")
    for name in ("track", "grade", "reflect", "publish", "uninstall-service"):
        sub.add_parser(name)
    isv = sub.add_parser("install-service", help="run floor + monitor under launchd (macOS)")
    isv.add_argument("--publish", action="store_true", help="also push the public snapshot every 15 minutes")
    lv = sub.add_parser("live")
    lv.add_argument("--interval-open", type=int, default=15, help="minutes between floor rounds, market open")
    lv.add_argument("--interval-closed", type=int, default=60, help="minutes between floor rounds, market closed")
    lv.add_argument("--schedule", default=None, help='e.g. "pick-US@09:05, pick-IN@09:20, ipo-IN@12:30" '
                    "(each time in that market's zone, weekdays)")
    lv.add_argument("--budget", type=float, default=10.0)
    lv.add_argument("--goal", type=float, default=100.0)
    lv.add_argument("--publish", action="store_true", help="push a read-only snapshot to GitHub Pages")
    lv.add_argument("--speakers", type=int, default=3, help="seats per floor round")
    sub.add_parser("stop-live")
    sub.add_parser("budget")
    sub.add_parser("monitor")
    sub.add_parser("mcp")
    p = sub.add_parser("ping")
    p.add_argument("--claude", action="store_true")
    p.add_argument("--rationed", action="store_true", help="also ping Codex/Claude (spends one ration call each)")
    sub.add_parser("portfolio")
    sub.add_parser("leaderboard")
    a = ap.parse_args(argv)

    if a.cmd in ("run", "ipo", "debate"):
        from .council import Council
        if a.cmd == "run":
            c = Council(budget=a.budget, horizon=a.horizon, rounds=a.rounds, include_claude=a.claude,
                        tickers=[t.strip() for t in a.tickers.split(",") if t.strip()],
                        open_paper_position=not a.no_position, market_code=a.market, target=a.target)
        elif a.cmd == "ipo":
            c = Council(budget=a.budget, rounds=a.rounds, mode="ipo", market_code=a.market, topic=a.topic)
        else:
            c = Council(rounds=a.rounds, mode="topic", market_code=a.market, topic=a.topic,
                        tickers=[t.strip() for t in a.tickers.split(",") if t.strip()])
        print(f"session {c.id}  (watch: council monitor -> http://127.0.0.1:8765)")
        stop = threading.Event()
        t = threading.Thread(target=_tail, args=(c.id, stop), daemon=True)
        t.start()
        try:
            c.run()
        finally:
            stop.set()
            t.join()
    elif a.cmd == "say":
        from .floor import say_to_floor
        say_to_floor(a.text)
        print("posted to the floor; the next speakers will answer you")
    elif a.cmd in ("track", "grade", "reflect"):
        from . import learning
        if a.cmd == "grade":
            print(f"{learning.grade()} calls matured")
        elif a.cmd == "reflect":
            print(json.dumps(learning.reflect(min_new=0), indent=2))
        else:
            learning.grade()
            print(json.dumps(learning.track_record(), indent=2, default=str))
    elif a.cmd in ("install-service", "uninstall-service"):
        from . import service
        done = service.install(publish=a.publish) if a.cmd == "install-service" else service.uninstall()
        print("\n".join(done) or "nothing to do")
    elif a.cmd == "publish":
        from . import publish
        print(publish.publish())
    elif a.cmd == "live":
        import atexit
        import os
        from . import floor
        if floor.running_pid() and floor.running_pid() != os.getpid():
            sys.exit(f"already live as pid {floor.running_pid()}; council stop-live first")
        floor.HOME.mkdir(parents=True, exist_ok=True)
        floor.PID_FILE.write_text(str(os.getpid()))
        atexit.register(lambda: floor.PID_FILE.unlink(missing_ok=True))
        print(f"floor is live (pid {os.getpid()}); watch at http://127.0.0.1:8765", flush=True)
        floor.Floor(a.interval_open, a.interval_closed, a.schedule or floor.DEFAULT_SCHEDULE, a.budget, a.speakers,
                    publish=a.publish or None, goal=a.goal).run()
    elif a.cmd == "stop-live":
        from . import floor
        print("stopping" if floor.stop_detached() else "not running")
    elif a.cmd == "budget":
        from . import llm, store
        for prov in ("codex", "claude"):
            print(f"{prov}: {store.usage_today(prov)} used, {llm.budget_left(prov)} left of {llm.daily_budget(prov)} today")
    elif a.cmd == "monitor":
        from . import monitor
        print("Wall Street Council monitor on http://127.0.0.1:8765")
        monitor.serve()
    elif a.cmd == "mcp":
        from .server import main as serve_mcp
        serve_mcp()
    elif a.cmd == "ping":
        from concurrent.futures import ThreadPoolExecutor
        from . import llm
        from .council import load_seats

        def one(s):
            try:
                r = llm.chat(s["model"], "Reply with the single word: ready", "Are you there?", max_tokens=400,
                             timeout=180)
                return s["name"], s["model"], "ok", r["ms"], r["text"][:40]
            except Exception as e:  # noqa: BLE001
                return s["name"], s["model"], "FAIL", 0, str(e)[:120]
        with ThreadPoolExecutor(10) as ex:
            seats = [s for s in load_seats(a.claude)
                     if a.rationed or s["model"].split("/")[0] not in ("codex", "claude")]
            for row in ex.map(one, seats):
                print("{:<16} {:<48} {:<5} {:>6}ms  {}".format(*row))
    elif a.cmd == "portfolio":
        from .scoreboard import portfolio
        print(json.dumps(portfolio(), indent=2, default=str))
    elif a.cmd == "leaderboard":
        from .scoreboard import leaderboard
        print(json.dumps(leaderboard(), indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
