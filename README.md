# SpaceTraders CLI

A terminal-based client for the [SpaceTraders](https://spacetraders.io) API game.

## Quick Start

```bash
# 1. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the game
python main.py
```

On first run you'll be prompted to either **register a new agent** or **paste an existing token**.  
Your token is saved to a `.env` file automatically.

## Features

| Menu | Actions |
|------|---------|
| **Agent info** | View credits, HQ, ship count |
| **Fleet** | List ships, navigate, orbit/dock, refuel, extract, siphon, survey, buy/sell cargo, refine, chart, repair, scrap, buy new ships |
| **Contracts** | List, accept, deliver cargo, fulfill, negotiate new |
| **Universe** | Browse waypoints, markets, shipyards, jump gates, factions |
| **Server status** | Game stats and top agent leaderboard |

## Project Structure

```
main.py          ← Entry point & menus
client.py        ← Base HTTP client (auth, error handling, pagination)
agent.py         ← Agent/account endpoints
fleet.py         ← Ship & fleet endpoints
contracts.py     ← Contract endpoints
universe.py      ← Systems, waypoints, markets, shipyards, factions
display.py       ← Rich terminal UI helpers
requirements.txt
.env             ← Your token (auto-created, never commit this)
```

## API Reference

- Docs: https://spacetraders.io/openapi  
- Getting started: https://spacetraders.io/getting-started
