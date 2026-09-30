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
    page = resources.files("wallstreet_council").joinpath("monitor.html").read_text()
    (out / "index.html").write_text(page.replace("/*STATIC*/false", "true"))
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
