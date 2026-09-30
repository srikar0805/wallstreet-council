"""Run the floor and the monitor as macOS LaunchAgents: they start at login, restart if they crash,
and keep going when no terminal is open.

    council install-service [--publish]   write and load ~/Library/LaunchAgents/com.wallstreet-council.*.plist
    council uninstall-service             unload and remove them
    council stop-live / start-live        pause or resume just the floor (the monitor keeps running);
                                          a paused floor comes back at the next login
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

from .floor import HOME

AGENTS = Path.home() / "Library" / "LaunchAgents"
LABELS = {"live": "com.wallstreet-council.live", "monitor": "com.wallstreet-council.monitor"}
PROJECT = Path(__file__).resolve().parents[2]


def _uv() -> str:
    return shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")


def _plist(label: str, args: list[str], env: dict[str, str]) -> dict:
    return {
        "Label": label, "ProgramArguments": [_uv(), "run", "--project", str(PROJECT), "council", *args],
        "WorkingDirectory": str(PROJECT), "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 60,
        "StandardOutPath": str(HOME / f"{label.split('.')[-1]}.log"),
        "StandardErrorPath": str(HOME / f"{label.split('.')[-1]}.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:"
                                         + str(Path.home() / ".local" / "bin"), "HOME": str(Path.home()), **env},
    }


def install(publish: bool = False, extra_live_args: list[str] | None = None) -> list[str]:
    AGENTS.mkdir(parents=True, exist_ok=True)
    HOME.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k.startswith("COUNCIL_")}
    if publish:
        env["COUNCIL_PUBLISH"] = "1"
    done = []
    for key, args in (("live", ["live", *(extra_live_args or [])]), ("monitor", ["monitor"])):
        path = AGENTS / f"{LABELS[key]}.plist"
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        path.write_bytes(plistlib.dumps(_plist(LABELS[key], args, env)))
        subprocess.run(["launchctl", "load", "-w", str(path)], check=True, capture_output=True)
        done.append(str(path))
    return done


def uninstall() -> list[str]:
    done = []
    for label in LABELS.values():
        path = AGENTS / f"{label}.plist"
        if path.exists():
            subprocess.run(["launchctl", "unload", "-w", str(path)], capture_output=True)
            path.unlink()
            done.append(str(path))
    return done


def live_installed() -> bool:
    return (AGENTS / f"{LABELS['live']}.plist").exists()


def pause_live() -> None:
    """Unload the floor agent so launchd stops restarting it. The plist stays, so it resumes at next login."""
    subprocess.run(["launchctl", "unload", str(AGENTS / f"{LABELS['live']}.plist")], capture_output=True)


def resume_live() -> None:
    subprocess.run(["launchctl", "load", str(AGENTS / f"{LABELS['live']}.plist")], capture_output=True, check=True)
