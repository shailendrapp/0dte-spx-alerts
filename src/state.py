"""State management: read/write `state/today.json` and commit to repo.

State schema (all fields optional except `date`):

    {
      "date": "2026-05-12",
      "decision": "TRADE" | "SKIP" | null,
      "skip_reason": "...",                # only when decision=SKIP
      "trade": {
        "S0": 7420.5, "EM": 58.0,
        "Kp": 7360, "Lp": 7335, "Kc": 7480, "Lc": 7505,
        "credit": 5.30, "max_loss_per_contract": 1970,
        "underlying": "SPX",
        "leg_symbols": {                   # exact Tradier symbols for the 4 legs
          "Kp": "SPXW...", "Lp": "...", "Kc": "...", "Lc": "..."
        }
      },
      "alerts_fired": {
        "breach_put": false, "breach_call": false,
        "pt_50": false, "loss_150": false, "eod_recap": false
      },
      "intraday_log": [ {time, mark, spx} ... ]
    }

The file is committed by GitHub Actions using the default GITHUB_TOKEN with
contents:write permission.  In local dev (DRY_RUN=1) commits are skipped.
"""
from __future__ import annotations
import json
import logging
import os
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "state"
STATE_FILE = STATE_DIR / "today.json"
HISTORY_FILE = STATE_DIR / "history.jsonl"

log = logging.getLogger(__name__)


def _ensure_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def empty_state(today: date) -> dict[str, Any]:
    return {
        "date": today.isoformat(),
        "decision": None,
        "skip_reason": None,
        "trade": None,
        "alerts_fired": {
            "breach_put": False,
            "breach_call": False,
            "pt_50": False,
            "loss_150": False,
            "eod_recap": False,
        },
        "intraday_log": [],
    }


def read_state() -> dict[str, Any] | None:
    """Return parsed today.json, or None if it doesn't exist."""
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError as e:
        log.error("state/today.json is malformed: %s", e)
        return None


def write_state(state: dict[str, Any]) -> None:
    _ensure_dir()
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    log.info("state/today.json updated")


def archive_to_history(state: dict[str, Any]) -> None:
    """Append a finalized day's state to history.jsonl."""
    _ensure_dir()
    with HISTORY_FILE.open("a") as f:
        f.write(json.dumps(state, default=str) + "\n")


def git_commit_state(message: str, dry_run: bool = False) -> None:
    """Commit the state directory back to the repo (CI only)."""
    if dry_run:
        log.info("DRY_RUN -- skipping git commit")
        return
    if not shutil.which("git"):
        log.warning("git not on PATH; skipping commit")
        return

    actor = os.environ.get("GITHUB_ACTOR", "0dte-bot")
    email = f"{actor}@users.noreply.github.com"

    try:
        subprocess.run(["git", "config", "user.name", actor], check=True, cwd=REPO_ROOT)
        subprocess.run(["git", "config", "user.email", email], check=True, cwd=REPO_ROOT)
        subprocess.run(["git", "add", "state/"], check=True, cwd=REPO_ROOT)

        # Skip if nothing changed
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=REPO_ROOT
        )
        if diff.returncode == 0:
            log.info("No state changes to commit")
            return

        subprocess.run(["git", "commit", "-m", message], check=True, cwd=REPO_ROOT)
        subprocess.run(["git", "push"], check=True, cwd=REPO_ROOT)
        log.info("State committed and pushed: %s", message)
    except subprocess.CalledProcessError as e:
        log.error("git commit failed: %s", e)
        # Non-fatal: keep going so the alert still fires.
