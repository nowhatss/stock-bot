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


def _query_python_processes(timeout: int = 15) -> list[dict]:
    """Live OS process scan instead of a JSON tracking file -- a JSON file only
    knows about processes *this controller* launched, so it'd miss a bot you
    started manually in a terminal, from the Startup-folder shortcut, or via a
    previous controller run. Scanning live processes catches all of those, so
    /start can correctly refuse to launch a second copy of the same bot no
    matter how the first one was started (two processes writing the same
    state.json at once would corrupt it)."""
    ps_cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" "
        "| Select-Object ProcessId,CommandLine,CreationDate | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
        data = json.loads(out.stdout or "[]")
    except Exception:
        return []
    if isinstance(data, dict):  # PowerShell unwraps a single match to an object, not a list
        data = [data]
    return data


def _parse_cim_date(raw: str | None) -> str:
    """CIM CreationDate comes back from ConvertTo-Json as '/Date(<ms since epoch>)/'."""
    if not raw:
        return "unknown"
    try:
        ms = int(raw.split("(")[-1].split(")")[0].split("+")[0].split("-")[0])
        return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).isoformat()
    except Exception:
        return "unknown"


def _find_running(script_path: str) -> dict | None:
    """Matches by script *basename* (not full path) so this catches a bot
    started with a relative path (e.g. `python grid_bot.py` run from inside
    the folder) as well as an absolute one. The three controlled scripts'
    basenames don't collide with each other (grid_bot.py / trend_bot.py /
    grid_bot_watchlist.py all have distinct suffixes right after the shared
    prefix), so a substring match is unambiguous here."""
    needle = os.path.basename(script_path).lower()
    for proc in _query_python_processes():
        cmdline = (proc.get("CommandLine") or "").lower()
        if needle in cmdline:
            return {
                "pid": proc["ProcessId"],
                "started_at": _parse_cim_date(proc.get("CreationDate")),
            }
    return None


def _start_process(key: str) -> str:
    label = BOTS[key]["label"]
    running = _find_running(BOTS[key]["script"])
    if running:
        return (
            f"{label} is already running (PID {running['pid']}) -- started manually, via "
            f"a previous /start, or at Windows logon. Refusing to start a second copy: two "
            f"instances writing the same state.json at once would corrupt it."
        )

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
    return (
        f"Started {label} (PID {proc.pid}). If it exits immediately (bad config, "
        f"missing token, etc.) `/status` will show it as not running -- check "
        f"logs/controller_launch_{key}.log for why."
    )


def _stop_process(key: str) -> str:
    label = BOTS[key]["label"]
    running = _find_running(BOTS[key]["script"])
    if not running:
        return f"{label} is not running."

    pid = running["pid"]
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, timeout=10)
    return (
        f"Stopped {label} (PID {pid}). Its state.json was already saved as of its "
        f"last poll, so nothing is lost -- but this is a hard kill, not the graceful "
        f"Ctrl+C shutdown you'd get stopping it yourself, so it won't post its own "
        f"'stopped' Discord message. This reply is that confirmation instead."
    )


def _status_line(key: str) -> str:
    label = BOTS[key]["label"]
    running = _find_running(BOTS[key]["script"])
    if running:
        return f"🟢 **{label}** -- running (PID {running['pid']}, started {running['started_at']})"
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
