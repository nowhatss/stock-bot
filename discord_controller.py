#!/usr/bin/env python3
"""
Discord remote control for the trading bots -- start/stop/status only.

This is a SEPARATE Discord bot from the notification webhook the other bots
already use. A webhook can only send messages; receiving slash commands from
your phone needs a real bot connection (a "gateway" bot) instead. This
process:
  - never places or touches any order itself, and never imports the trading
    engines -- it only launches/kills the existing bot *scripts* as plain
    subprocesses, exactly as if you'd typed `python grid_bot.py` yourself
  - only acts on commands from the single Discord user ID configured as
    owner_id below; every other user gets "Not authorized."
  - needs no inbound port/forwarding -- like the bots' webhook calls, it only
    makes an outbound connection to Discord

Setup: see the "Remote control via Discord" section in README.md.

This process has to be running on your PC for phone commands to reach it --
if your PC is off or asleep, /start /stop /status won't work until it's back
and you've restarted `python discord_controller.py`.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from typing import Optional

import discord
from discord import app_commands

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "discord_controller_config.json")
LOG_DIR = os.path.join(HERE, "logs")
STATE_PATH = os.path.join(LOG_DIR, "controller_state.json")

BOTS = {
    "cryptogrid": {
        "label": "Crypto grid bot",
        "script": os.path.join(HERE, "grid_bot.py"),
        "cwd": HERE,
    },
    "cryptotrend": {
        "label": "Crypto trend bot",
        "script": os.path.join(HERE, "trend_bot.py"),
        "cwd": HERE,
    },
    "watchlist": {
        "label": "Stock watchlist bot",
        "script": os.path.join(HERE, "stock_bots", "grid_bot_watchlist.py"),
        "cwd": os.path.join(HERE, "stock_bots"),
    },
}

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------- #
# config + process bookkeeping
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit(
            f"Missing {CONFIG_PATH}. Copy discord_controller_config.example.json to "
            f"discord_controller_config.json and fill in bot_token / owner_id -- see "
            f"README.md, 'Remote control via Discord'."
        )
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not cfg.get("bot_token"):
        raise SystemExit(f"{CONFIG_PATH}: bot_token is empty.")
    if not cfg.get("owner_id"):
        raise SystemExit(f"{CONFIG_PATH}: owner_id is empty.")
    return cfg


def _load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_PATH)


def _pid_alive(pid: int) -> bool:
    """No extra dependency (e.g. psutil) -- shells out to tasklist, which
    ships with every Windows install."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return False
    return str(pid) in out.stdout


def _start_process(key: str) -> str:
    state = _load_state()
    existing = state.get(key)
    label = BOTS[key]["label"]
    if existing and _pid_alive(existing["pid"]):
        return f"{label} is already running (PID {existing['pid']})."

    spec = BOTS[key]
    os.makedirs(LOG_DIR, exist_ok=True)
    launch_log = os.path.join(LOG_DIR, f"controller_launch_{key}.log")
    with open(launch_log, "a", encoding="utf-8") as lf:
        lf.write(f"\n--- launched {dt.datetime.now(dt.timezone.utc).isoformat()} ---\n")
        lf.flush()
        proc = subprocess.Popen(
            [sys.executable, spec["script"]],
            cwd=spec["cwd"],
            stdout=lf,
            stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
    state[key] = {
        "pid": proc.pid,
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    _save_state(state)
    return (
        f"Started {label} (PID {proc.pid}). If it exits immediately (bad config, "
        f"missing token, etc.) `/status` will show it as not running -- check "
        f"logs/controller_launch_{key}.log for why."
    )


def _stop_process(key: str) -> str:
    state = _load_state()
    existing = state.get(key)
    label = BOTS[key]["label"]
    if not existing or not _pid_alive(existing["pid"]):
        state.pop(key, None)
        _save_state(state)
        return f"{label} is not running."

    pid = existing["pid"]
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, timeout=10)
    state.pop(key, None)
    _save_state(state)
    return (
        f"Stopped {label} (PID {pid}). Its state.json was already saved as of its "
        f"last poll, so nothing is lost -- but this is a hard kill, not the graceful "
        f"Ctrl+C shutdown you'd get stopping it yourself, so it won't post its own "
        f"'stopped' Discord message. This reply is that confirmation instead."
    )


def _status_line(key: str) -> str:
    state = _load_state()
    existing = state.get(key)
    label = BOTS[key]["label"]
    if existing and _pid_alive(existing["pid"]):
        return f"🟢 **{label}** -- running (PID {existing['pid']}, started {existing['started_at']})"
    return f"⚪ **{label}** -- not running"


# --------------------------------------------------------------------------- #
# Discord bot
# --------------------------------------------------------------------------- #
intents = discord.Intents.none()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

BOT_CHOICES = [app_commands.Choice(name=spec["label"], value=key) for key, spec in BOTS.items()]


def _authorized(interaction: discord.Interaction) -> bool:
    return interaction.user.id == client.controller_owner_id


@tree.command(name="start", description="Start one of the dry-run trading bots")
@app_commands.choices(bot=BOT_CHOICES)
async def start_cmd(interaction: discord.Interaction, bot: app_commands.Choice[str]):
    if not _authorized(interaction):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.send_message(_start_process(bot.value))


@tree.command(name="stop", description="Stop one of the dry-run trading bots")
@app_commands.choices(bot=BOT_CHOICES)
async def stop_cmd(interaction: discord.Interaction, bot: app_commands.Choice[str]):
    if not _authorized(interaction):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.send_message(_stop_process(bot.value))


@tree.command(name="status", description="Check whether the trading bots are running")
@app_commands.choices(bot=BOT_CHOICES)
async def status_cmd(interaction: discord.Interaction, bot: Optional[app_commands.Choice[str]] = None):
    if not _authorized(interaction):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    keys = [bot.value] if bot is not None else list(BOTS.keys())
    await interaction.response.send_message("\n".join(_status_line(k) for k in keys))


@client.event
async def on_ready():
    guild_id = client.controller_guild_id
    if guild_id:
        guild_obj = discord.Object(id=guild_id)
        tree.copy_global_to(guild=guild_obj)
        await tree.sync(guild=guild_obj)
    else:
        await tree.sync()
    print(f"Discord controller ready as {client.user}. Commands: /start /stop /status")


def main() -> int:
    cfg = load_config()
    client.controller_owner_id = int(cfg["owner_id"])
    client.controller_guild_id = int(cfg["guild_id"]) if cfg.get("guild_id") else None
    client.run(cfg["bot_token"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
