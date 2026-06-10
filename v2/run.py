"""
run.py — Async entry point for v2.
Initialises the DB, wires up config + orchestrator, and starts the bot.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys

import db
from orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# ANSI colour helpers
# ---------------------------------------------------------------------------

_RESET  = "\x1b[0m"
_BOLD   = "\x1b[1m"
_DIM    = "\x1b[2m"

# Colours cycled per ship suffix (TM2-1, TM2-3, …).
# Each tuple: (normal, bold variant for the ship-name prefix)
_SHIP_COLOURS = [
    ("\x1b[36m",  "\x1b[96m"),   # cyan
    ("\x1b[32m",  "\x1b[92m"),   # green
    ("\x1b[35m",  "\x1b[95m"),   # magenta
    ("\x1b[33m",  "\x1b[93m"),   # yellow
    ("\x1b[34m",  "\x1b[94m"),   # blue
    ("\x1b[31m",  "\x1b[91m"),   # red
    ("\x1b[37m",  "\x1b[97m"),   # white
]

_LEVEL_COLOURS = {
    "DEBUG":    "\x1b[2m",          # dim
    "INFO":     "",                 # default
    "WARNING":  "\x1b[33m",        # yellow
    "ERROR":    "\x1b[31m\x1b[1m", # bold red
    "CRITICAL": "\x1b[31m\x1b[1m",
}

# Cache: extracted ship suffix (e.g. "1", "3") → colour index
_ship_colour_cache: dict[str, int] = {}
_next_colour_idx = 0


def _ship_colour(logger_name: str) -> tuple[str, str] | None:
    """Return (normal, bold) ANSI codes for the ship in logger_name, or None."""
    global _next_colour_idx
    # MinerRole.TYLERMASTERY2-3  or  SiphonerRole.TYLERMASTERY2-6  etc.
    m = re.search(r'([A-Z0-9]+-\d+)$', logger_name)
    if not m:
        return None
    key = m.group(1)
    if key not in _ship_colour_cache:
        _ship_colour_cache[key] = _next_colour_idx % len(_SHIP_COLOURS)
        _next_colour_idx += 1
    return _SHIP_COLOURS[_ship_colour_cache[key]]


class ColourFormatter(logging.Formatter):
    """Logging formatter that colours each ship's output differently."""

    def format(self, record: logging.LogRecord) -> str:
        ts    = self.formatTime(record, "%H:%M:%S")
        level = record.levelname
        name  = record.name
        msg   = record.getMessage()

        level_col = _LEVEL_COLOURS.get(level, "")
        ship_col  = _ship_colour(name)

        if ship_col:
            norm, bold = ship_col
            # "HH:MM:SS Name LEVEL message"
            out = (
                f"{_DIM}{ts}{_RESET} "
                f"{bold}{name}{_RESET} "
                f"{level_col}{level}{_RESET} "
                f"{norm}{msg}{_RESET}"
            )
        else:
            out = (
                f"{_DIM}{ts}{_RESET} "
                f"{name} "
                f"{level_col}{level}{_RESET} "
                f"{msg}"
            )

        if record.exc_info:
            out += "\n" + self.formatException(record.exc_info)
        return out


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColourFormatter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)


def main() -> None:
    _setup_logging()
    # Quiet noisy third-party loggers
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    db.init_db()
    orchestrator = Orchestrator()
    try:
        asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Stopped by user (KeyboardInterrupt)")
        sys.exit(0)


if __name__ == "__main__":
    main()
