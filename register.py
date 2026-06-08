#!/usr/bin/env python3
"""
One-shot registration script for a new SpaceTraders reset cycle.

Usage:
    python3 register.py

Registers agent MASTERY, writes the token to .env, and prints startup info.
After running, update SYSTEM / ASTEROID / ASTEROID_BASE in play.py if your
starting system differs from the existing defaults.
"""
import os
from pathlib import Path

from dotenv import set_key

import agent as agent_api

ENV_FILE   = str(Path(__file__).parent / ".env")
AGENT_NAME = "MASTERY"
FACTION    = "COSMIC"


def main() -> None:
    print(f"Registering agent '{AGENT_NAME}' with faction '{FACTION}'...")
    try:
        result = agent_api.register(AGENT_NAME, FACTION)
    except Exception as e:
        print(f"\nRegistration failed: {e}")
        print("If the symbol is already taken, edit AGENT_NAME at the top of this file.")
        return

    token    = result.get("token", "")
    ag       = result.get("agent", {})
    ships    = result.get("ships", [])
    contract = result.get("contract", {})

    if not token:
        print("No token returned — registration may have failed.")
        return

    # Persist token
    set_key(ENV_FILE, "SPACETRADERS_TOKEN", token)
    os.environ["SPACETRADERS_TOKEN"] = token

    print(f"\n✓ Registered successfully!")
    print(f"  Agent:    {ag.get('symbol')}")
    print(f"  Credits:  {ag.get('credits', 0):,}")
    print(f"  HQ:       {ag.get('headquarters')}")
    print(f"  Ships:    {len(ships)}")
    for s in ships:
        nav = s.get("nav", {})
        print(f"    {s['symbol']}  role={s['registration']['role']}  @ {nav.get('waypointSymbol', '?')}")

    if contract:
        d = contract.get("terms", {}).get("deliver", [{}])[0]
        payout = contract.get("terms", {}).get("payment", {})
        print(
            f"  Contract: deliver {d.get('unitsRequired')}x {d.get('tradeSymbol')}"
            f" → {d.get('destinationSymbol')}"
            f"  (+{payout.get('onFulfilled', 0):,} cr on fulfill)"
        )

    print(f"\n  Token written to .env")

    hq = ag.get("headquarters", "")
    if hq:
        parts  = hq.split("-")
        system = f"{parts[0]}-{parts[1]}" if len(parts) >= 2 else hq
        print(f"\n  Starting system : {system}")
        print(f"  HQ waypoint     : {hq}")
        print(f"\n  !! If starting system differs from the current SYSTEM constant in")
        print(f"  !! play.py, update SYSTEM, ASTEROID, ASTEROID_BASE, SHIPYARD_WP,")
        print(f"  !! SHIPYARD_WPS, COMMAND_SHIP, and FLEET_MANAGER_SHIP before running.")


if __name__ == "__main__":
    main()
