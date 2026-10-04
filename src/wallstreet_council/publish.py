"""Publish a read-only snapshot of the council to GitHub Pages.

The same monitor page runs in static mode against JSON files. Each publish is a normal commit on the
`gh-pages` branch, never a force-push, so the branch history is a public, timestamped ledger: every
ruling is visible there before its outcome is known, and nobody (including the owner) can quietly
rewrite a bad call without the rewrite showing.

    COUNCIL_SITE_REMOTE   git remote to push to (default: this checkout's `origin`)
    COUNCIL_PUBLISH_CLIENT=0   leave the client's own chat messages out of the public site
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from importlib import resources
from pathlib import Path

from . import learning, llm, scoreboard, store

SITE = Path(os.environ.get("COUNCIL_SITE_DIR", Path.home() / ".wallstreet-council" / "site"))
REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_EVENTS = 400
BULKY = ("dossier", "dossiers", "regime", "clock", "clock_us", "clock_in")


def _git(*args: str, cwd: Path = SITE, check: bool = True) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check).stdout.strip()


def _remote() -> str:
    return os.environ.get("COUNCIL_SITE_REMOTE") or _git("remote", "get-url", "origin", cwd=REPO_ROOT)


def _ensure_checkout() -> None:
    if (SITE / ".git").exists():
        _git("fetch", "origin", "gh-pages", check=False)
        if _git("rev-parse", "--verify", "origin/gh-pages", check=False):
            _git("reset", "--hard", "origin/gh-pages", check=False)
        return
    SITE.mkdir(parents=True, exist_ok=True)
    _git("init", "-q")
    _git("remote", "add", "origin", _remote())
    _git("fetch", "origin", "gh-pages", check=False)
    if _git("rev-parse", "--verify", "origin/gh-pages", check=False):
        _git("checkout", "-q", "-B", "gh-pages", "origin/gh-pages")
    else:
        _git("checkout", "-q", "--orphan", "gh-pages")


def _slim(e: dict, show_client: bool) -> dict | None:
    if e["kind"] == "user" and not show_client:
        return None
    d = e.get("data")
    if isinstance(d, dict):
        d = {k: v for k, v in d.items() if k not in BULKY}
    return {**{k: e[k] for k in ("seq", "ts", "phase", "speaker", "model", "kind", "text")}, "data": d}


def build(out: Path = SITE) -> dict:
    show_client = os.environ.get("COUNCIL_PUBLISH_CLIENT", "1") != "0"
    (out / "data" / "events").mkdir(parents=True, exist_ok=True)
    pkg = resources.files("wallstreet_council")
    # Front page: plain language for someone who is not a trader. Full monitor one click away.
    (out / "index.html").write_text(pkg.joinpath("simple.html").read_text())
    (out / "details.html").write_text(pkg.joinpath("monitor.html").read_text().replace("/*STATIC*/false", "true"))
    (out / ".nojekyll").write_text("")
    sessions = store.sessions(60)
    keep = set()
    for s in sessions:
        evs = [x for x in (_slim(e, show_client) for e in store.last_events(s["id"], MAX_EVENTS)) if x]
        (out / "data" / "events" / f"{s['id']}.json").write_text(json.dumps(evs, default=str))
        keep.add(f"{s['id']}.json")
    for f in (out / "data" / "events").glob("*.json"):
        if f.name not in keep:
            f.unlink()
    pub = [{k: s.get(k) for k in ("id", "created", "status", "budget", "horizon", "mode", "market", "topic", "verdict")}
           | {"seats": []} for s in sessions]
    if not show_client:
        for s in pub:
            s["topic"] = None
    (out / "data" / "sessions.json").write_text(json.dumps(pub, default=str))
    floor = next((s for s in sessions if s["id"].startswith("floor-")), None)
    if floor:
        from . import memory
        try:
            from .council import DEFAULT_BUDGET
            facts = [f"Practice money: ${DEFAULT_BUDGET['US']:,.0f} in the US and Rs {DEFAULT_BUDGET['IN']:,.0f} in India."]
            for mkt, p in picks_of_the_day(sessions).items():
                nm = lambda t: str(t or "").split(".")[0]  # noqa: E731
                where = "America" if mkt == "US" else "India"
                what = (f"buy {nm(p['ticker'])}" if p["status"] == "BUY"
                        else f"wait for now (if buying anyway, their favourite is {nm(p['backup_pick'])})")
                facts.append(f"Today's pick in {where}: {what}. Reason: {p['one_line']}")
            plain = memory.plain_summary(floor["id"], context="\n".join(facts))
        except Exception:  # noqa: BLE001
            plain = {}
        (out / "data" / "plain.json").write_text(json.dumps(plain))
    picks = picks_of_the_day(sessions)
    (out / "data" / "picks.json").write_text(json.dumps(picks, default=str))
    try:
        from . import congress
        sc = congress.scorecard()
        (out / "data" / "congress.json").write_text(json.dumps({
            "overall": sc.get("overall"), "recent": sc.get("recent", [])[:15], "hot": congress.hot_tickers()[:8],
            "best": sc.get("filers", [])[:5], "worst": sc.get("filers", [])[-5:][::-1], "as_of": sc.get("as_of"),
            "fair_test": congress.fair_test()},
            default=str))
    except Exception:  # noqa: BLE001  the page simply hides the section
        pass
    # Stock cards for the picks and watchlist only, built in parallel and cached for two hours.
    from concurrent.futures import ThreadPoolExecutor
    from . import stockcard
    (out / "data" / "stocks").mkdir(parents=True, exist_ok=True)
    # Today's picks and backup picks always get a card, then the watchlist, six stocks at most.
    first = [x for p in picks.values() for x in (p.get("ticker"), p.get("backup_pick")) if x]
    syms = list(dict.fromkeys(first + stockcard.tickers_to_show(sessions)))[:6]
    with ThreadPoolExecutor(4) as ex:
        cards = [c for c in ex.map(stockcard.cached_build, syms) if not c.get("error")]
    for c in cards:
        (out / "data" / "stocks" / f"{c['symbol']}.json").write_text(stockcard.dump(c))
    (out / "data" / "stocks.json").write_text(json.dumps([{"symbol": c["symbol"], "name": c["name"], "market": c["market"]}
                                                          for c in cards]))
    learning.grade()
    track = learning.track_record()
    (out / "data" / "track.json").write_text(json.dumps(track, default=str))
    (out / "data" / "portfolio.json").write_text(json.dumps(scoreboard.portfolio(), default=str))
    (out / "data" / "budget.json").write_text(json.dumps({
        "published_at": time.time(), "live_pid": None,
        "codex": {"used": store.usage_today("codex"), "per_day": llm.daily_budget("codex")}}))
    return {"sessions": len(sessions), "graded_rulings": (track.get("all_graded") or {}).get("calls", 0)}


def publish() -> str:
    _ensure_checkout()
    info = build(SITE)
    _git("add", "-A")
    if not _git("status", "--porcelain"):
        return "nothing changed"
    stamp = time.strftime("%Y-%m-%d %H:%M %Z")
    _git("commit", "-q", "-m", f"Snapshot {stamp}: {info['sessions']} sessions, {info['graded_rulings']} graded rulings")
    _git("push", "-q", "origin", "HEAD:gh-pages")
    return f"published {stamp}"


def picks_of_the_day(sessions: list[dict]) -> dict:
    """The latest stock-pick ruling per market, shaped for the front page. Older rulings that predate the plain
    fields fall back to their first sentence and the best-voted stock as the backup pick."""
    import re
    pos = {p["session"]: p for p in scoreboard.portfolio()["positions"]}
    out = {}
    for s in sessions:
        v = s.get("verdict") or {}
        mode, mkt = s.get("mode") or "pick", s.get("market") or "US"
        if mode != "pick" or mkt in out or not v:
            continue
        buy = v.get("decision") not in (None, "CASH", "NONE")
        backup = v.get("backup_pick") or next((k for k in (v.get("tally") or {}) if k not in ("CASH",)), None)
        first = re.split(r"(?<=[.!?])\s+", str(v.get("why") or ""))[0]
        p = pos.get(s["id"])
        out[mkt] = {"session": s["id"], "created": s["created"], "budget": s.get("budget"), "horizon": s.get("horizon"),
                    "status": "BUY" if buy else "WAIT", "ticker": v.get("decision") if buy else None,
                    "backup_pick": None if buy else backup, "one_line": v.get("one_line") or first,
                    "reasons": v.get("plain_reasons") or [], "good_news": v.get("good_news") or [],
                    "bad_news": v.get("bad_news") or [], "confidence": v.get("confidence"),
                    "paper": {"invested": p["dollars"], "now": p["value_now"], "pnl_pct": p["pnl_pct"]} if p else None}
    return out
