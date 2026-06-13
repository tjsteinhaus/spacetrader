"""
discord_notify.py — Fire-and-forget Discord webhook notifications for play.py.

Usage:
    import discord_notify as discord
    discord.send_status(ships, agent, cph_1h)
    discord.send_trade_start(ship, good, buy_wp, sell_wp, units, buy_price, est_profit)
    discord.send_trade_finish(ship, good, buy_wp, sell_wp, units, cost, revenue, profit)
    discord.send_contract_start(contract)
    discord.send_contract_finish(contract, earned)
    discord.send_shutdown(reason)
    discord.send_server_reset()
    discord.send_stuck(msg)

Webhook URL is read from the 'discord_webhook' bot_setting in the DB.
If not set, all calls are no-ops.
"""
from __future__ import annotations

import json
import os
import sys
import time
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import db

# ── Rate limiting ─────────────────────────────────────────────────────────────
# Discord allows 30 requests per 60s per webhook; we're well under that but
# add a small guard anyway.
_last_send_ts: float = 0.0
_send_lock = threading.Lock()
_MIN_INTERVAL = 1.0  # seconds between sends


def _get_webhook() -> str | None:
    return os.getenv("DISCORD_WEBHOOK") or db.get_bot_setting("discord_webhook") or None


def _post(payload: dict) -> None:
    """POST payload to Discord webhook. Non-blocking (runs in daemon thread)."""
    webhook = _get_webhook()
    if not webhook:
        return

    def _send():
        global _last_send_ts
        with _send_lock:
            gap = time.time() - _last_send_ts
            if gap < _MIN_INTERVAL:
                time.sleep(_MIN_INTERVAL - gap)
            try:
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    webhook,
                    data=data,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "DiscordBot (space-game, 1.0)",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    _ = resp.read()
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                print(f"[discord] HTTP {e.code}: {body}", file=sys.stderr)
            except Exception as e:
                print(f"[discord] send failed: {e}", file=sys.stderr)
            finally:
                _last_send_ts = time.time()

    t = threading.Thread(target=_send, daemon=True)
    t.start()


def _now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _embed(
    title: str,
    description: str = "",
    color: int = 0x58a6ff,
    fields: list[dict] | None = None,
    footer: str = "",
) -> dict:
    e: dict[str, Any] = {
        "title": title,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if description:
        e["description"] = description
    if fields:
        e["fields"] = fields
    if footer:
        e["footer"] = {"text": footer}
    return e


# ── Public API ────────────────────────────────────────────────────────────────

def send_status(
    ships: list[dict],
    agent: dict,
    cph_1h: int = 0,
    interval_min: int = 0,
) -> None:
    """Periodic fleet status table."""
    credits = agent.get("credits", 0)
    ship_count = len(ships)
    cph_sign = "+" if cph_1h >= 0 else ""
    cph_color_emoji = "🟢" if cph_1h >= 0 else "🔴"

    rows: list[str] = []
    for s in ships:
        sym   = s.get("symbol", "?")
        nav   = s.get("nav", {})
        fuel  = s.get("fuel", {})
        cargo = s.get("cargo", {})
        status = nav.get("status", "?")
        loc    = nav.get("waypointSymbol", "?")
        fuel_str  = f"{fuel.get('current',0)}/{fuel.get('capacity',1)}"
        cargo_str = f"{cargo.get('units',0)}/{cargo.get('capacity',1)}"
        row = f"`{sym:<18}` {status:<11} `{loc}` ⛽{fuel_str} 📦{cargo_str}"
        rows.append(row)

    desc = "\n".join(rows) if rows else "_No ships_"
    label = f"every {interval_min}min" if interval_min else "on demand"

    _post({"embeds": [_embed(
        title=f"📊 Fleet Status  •  {_now_str()}",
        description=desc,
        color=0x1f6feb,
        fields=[
            {"name": "Credits", "value": f"**{credits:,} cr**", "inline": True},
            {"name": f"{cph_color_emoji} CPH (1h)", "value": f"{cph_sign}{cph_1h:,} cr/h", "inline": True},
            {"name": "Ships", "value": str(ship_count), "inline": True},
        ],
        footer=f"SpaceTraders • {label}",
    )]})


def send_trade_start(
    ship: str,
    good: str,
    buy_wp: str,
    sell_wp: str,
    units: int,
    buy_price: int,
    est_profit: int,
) -> None:
    cost = units * buy_price
    _post({"embeds": [_embed(
        title=f"🛒 Trade Started — {good}",
        color=0x3fb950,
        fields=[
            {"name": "Ship",       "value": f"`{ship}`",        "inline": True},
            {"name": "Route",      "value": f"`{buy_wp}` → `{sell_wp}`", "inline": True},
            {"name": "Units",      "value": str(units),          "inline": True},
            {"name": "Buy price",  "value": f"{buy_price:,}/u",  "inline": True},
            {"name": "Total cost", "value": f"{cost:,} cr",      "inline": True},
            {"name": "Est. profit","value": f"+{est_profit:,} cr","inline": True},
        ],
        footer=f"SpaceTraders • {_now_str()}",
    )]})


def send_trade_finish(
    ship: str,
    good: str,
    buy_wp: str,
    sell_wp: str,
    units: int,
    cost: int,
    revenue: int,
    profit: int,
) -> None:
    color  = 0x3fb950 if profit >= 0 else 0xf85149
    sign   = "+" if profit >= 0 else ""
    emoji  = "💰" if profit >= 0 else "📉"
    _post({"embeds": [_embed(
        title=f"{emoji} Trade Finished — {good}",
        color=color,
        fields=[
            {"name": "Ship",    "value": f"`{ship}`",                  "inline": True},
            {"name": "Route",   "value": f"`{buy_wp}` → `{sell_wp}`",  "inline": True},
            {"name": "Units",   "value": str(units),                    "inline": True},
            {"name": "Cost",    "value": f"{cost:,} cr",               "inline": True},
            {"name": "Revenue", "value": f"{revenue:,} cr",            "inline": True},
            {"name": "Profit",  "value": f"**{sign}{profit:,} cr**",   "inline": True},
        ],
        footer=f"SpaceTraders • {_now_str()}",
    )]})


def send_contract_start(contract: dict) -> None:
    terms   = contract.get("terms", {})
    deliver = terms.get("deliver", [{}])[0]
    good    = deliver.get("tradeSymbol", "?")
    needed  = deliver.get("unitsRequired", 0)
    payout  = terms.get("payment", {}).get("onFulfilled", 0)
    deadline = terms.get("deadline", "")
    try:
        dl_dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        dl_str = dl_dt.strftime("%b %d %H:%M UTC")
    except Exception:
        dl_str = deadline[:19] if deadline else "?"

    _post({"embeds": [_embed(
        title="📋 Contract Started",
        color=0xe3b341,
        fields=[
            {"name": "Good",     "value": good,           "inline": True},
            {"name": "Required", "value": str(needed),    "inline": True},
            {"name": "Payout",   "value": f"{payout:,} cr", "inline": True},
            {"name": "Deadline", "value": dl_str,         "inline": True},
            {"name": "ID",       "value": f"`{contract.get('id','?')[:20]}`", "inline": True},
        ],
        footer=f"SpaceTraders • {_now_str()}",
    )]})


def send_contract_finish(contract: dict, earned: int) -> None:
    terms  = contract.get("terms", {})
    deliver = terms.get("deliver", [{}])[0]
    good   = deliver.get("tradeSymbol", "?")
    needed = deliver.get("unitsRequired", 0)
    _post({"embeds": [_embed(
        title="✅ Contract Fulfilled!",
        color=0x3fb950,
        description=f"Delivered **{needed}x {good}**",
        fields=[
            {"name": "Earned", "value": f"**{earned:,} cr**", "inline": True},
            {"name": "ID",     "value": f"`{contract.get('id','?')[:20]}`", "inline": True},
        ],
        footer=f"SpaceTraders • {_now_str()}",
    )]})


def send_shutdown(reason: str = "KeyboardInterrupt") -> None:
    _post({"embeds": [_embed(
        title="⛔ Bot Shutting Down",
        description=f"Reason: `{reason}`",
        color=0xf85149,
        footer=f"SpaceTraders • {_now_str()}",
    )]})
    # Give the daemon thread a moment to fire before process exits
    time.sleep(2)


def send_server_reset() -> None:
    _post({"embeds": [_embed(
        title="🔄 Server Reset Detected",
        description="The SpaceTraders server has reset. Re-registration required.",
        color=0xd29922,
        footer=f"SpaceTraders • {_now_str()}",
    )]})


def send_stuck(msg: str) -> None:
    _post({"embeds": [_embed(
        title="⚠️ Bot May Be Stuck",
        description=msg,
        color=0xd29922,
        footer=f"SpaceTraders • {_now_str()}",
    )]})


def send_scrap(ship: str, received: int) -> None:
    _post({"embeds": [_embed(
        title="🗑️ Ship Scrapped",
        color=0x8b949e,
        fields=[
            {"name": "Ship",     "value": f"`{ship}`",      "inline": True},
            {"name": "Received", "value": f"{received:,} cr", "inline": True},
        ],
        footer=f"SpaceTraders • {_now_str()}",
    )]})
