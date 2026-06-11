"""
client.py — Async SpaceTraders API client using aiohttp.
Handles auth, retries, rate limiting, pagination.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator

import aiohttp
from dotenv import load_dotenv, set_key

# Load token from the parent directory's .env (shared with v1)
_ENV_FILE = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_FILE)

BASE_URL = "https://api.spacetraders.io/v2/"

# ---------------------------------------------------------------------------
# Global rate limiter — SpaceTraders allows 2 req/s sustained, burst of 10.
# We use a token-bucket that refills at 2 tokens/s and caps at 10 burst tokens.
# All requests go through _rate_limit() before hitting the wire.
# ---------------------------------------------------------------------------

_RATE_TOKENS    = 10.0   # current bucket level (starts full)
_RATE_CAPACITY  = 10.0   # max burst
_RATE_REFILL    = 2.0    # tokens added per second
_RATE_LAST_TS   = 0.0    # monotonic time of last refill
_RATE_LOCK: asyncio.Lock | None = None  # created lazily inside the event loop


def _get_rate_lock() -> asyncio.Lock:
    global _RATE_LOCK
    if _RATE_LOCK is None:
        _RATE_LOCK = asyncio.Lock()
    return _RATE_LOCK


async def _rate_limit() -> None:
    """Consume one token from the bucket, sleeping if the bucket is empty."""
    global _RATE_TOKENS, _RATE_LAST_TS
    lock = _get_rate_lock()
    async with lock:
        now = time.monotonic()
        if _RATE_LAST_TS == 0.0:
            _RATE_LAST_TS = now
        elapsed = now - _RATE_LAST_TS
        _RATE_TOKENS = min(_RATE_CAPACITY, _RATE_TOKENS + elapsed * _RATE_REFILL)
        _RATE_LAST_TS = now

        if _RATE_TOKENS < 1.0:
            wait = (1.0 - _RATE_TOKENS) / _RATE_REFILL
            await asyncio.sleep(wait)
            _RATE_TOKENS = 0.0
            _RATE_LAST_TS = time.monotonic()
        else:
            _RATE_TOKENS -= 1.0


class SpaceTradersError(Exception):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class SpaceTradersClient:
    """Async HTTP client for the SpaceTraders v2 API.

    Usage:
        async with SpaceTradersClient() as client:
            agent = await client.get("/my/agent")
    """

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.getenv("SPACETRADERS_TOKEN") or ""
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "SpaceTradersClient":
        self._session = aiohttp.ClientSession(
            base_url=BASE_URL,
            headers=self._make_headers(),
            timeout=aiohttp.ClientTimeout(total=30),
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    def _make_headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _handle_response(self, resp: aiohttp.ClientResponse) -> dict[str, Any]:
        if resp.status == 429:
            retry_after = float(resp.headers.get("Retry-After", 1))
            await asyncio.sleep(retry_after)
            raise SpaceTradersError(429, "Rate limited")

        # 204 No Content — valid empty success (e.g. cooldown endpoint when no cooldown)
        if resp.status == 204:
            return {}

        # For error responses, raise without parsing body
        if not resp.ok:
            try:
                data = await resp.json(content_type=None)
                error = (data or {}).get("error", {}) if isinstance(data, dict) else {}
            except Exception:
                error = {}
            raise SpaceTradersError(
                error.get("code", resp.status),
                error.get("message", str(resp.status)),
            )

        # Successful response — parse JSON
        try:
            data = await resp.json(content_type=None)
        except Exception:
            return {}

        if not isinstance(data, dict):
            return {}

        return data.get("data", data)

    async def _handle_response_raw(self, resp: aiohttp.ClientResponse) -> dict[str, Any]:
        """Like _handle_response but returns the full JSON including 'meta'."""
        if resp.status == 429:
            retry_after = float(resp.headers.get("Retry-After", 1))
            await asyncio.sleep(retry_after)
            raise SpaceTradersError(429, "Rate limited")

        if resp.status == 204:
            return {}

        try:
            data = await resp.json(content_type=None)
        except Exception:
            resp.raise_for_status()
            return {}

        if data is None:
            if resp.ok:
                return {}
            raise SpaceTradersError(resp.status, "Empty error response")

        if resp.ok:
            return data  # full response — NOT stripped

        error = data.get("error", {})
        raise SpaceTradersError(
            error.get("code", resp.status),
            error.get("message", await resp.text()),
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        retries: int = 5,
    ) -> dict[str, Any]:
        assert self._session is not None, "Client not started — use async with"
        delay = 5
        non_rate_limit_attempts = 0
        rate_limit_attempts = 0
        while non_rate_limit_attempts < retries:
            try:
                await _rate_limit()
                async with self._session.request(
                    method, path, params=params, json=json,
                ) as resp:
                    return await self._handle_response(resp)
            except SpaceTradersError as e:
                if e.code == 429:
                    rate_limit_attempts += 1
                    if rate_limit_attempts > 10:
                        raise  # stop hammering after 10 rate-limit hits
                    # Back off proportionally to how many times we've been limited
                    await asyncio.sleep(rate_limit_attempts * 0.5)
                    continue
                non_rate_limit_attempts += 1
                if non_rate_limit_attempts < retries:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)
                else:
                    raise
            except (aiohttp.ClientError, asyncio.TimeoutError):
                non_rate_limit_attempts += 1
                if non_rate_limit_attempts < retries:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)
                else:
                    raise

        raise RuntimeError("Request loop exited without returning")  # unreachable

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def _get_raw(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET that returns the full JSON response (preserving 'meta')."""
        assert self._session is not None, "Client not started — use async with"
        delay = 5
        non_rate_limit_attempts = 0
        rate_limit_attempts = 0
        retries = 5
        while non_rate_limit_attempts < retries:
            try:
                await _rate_limit()
                async with self._session.request("GET", path, params=params) as resp:
                    return await self._handle_response_raw(resp)
            except SpaceTradersError as e:
                if e.code == 429:
                    rate_limit_attempts += 1
                    if rate_limit_attempts > 10:
                        raise
                    await asyncio.sleep(rate_limit_attempts * 0.5)
                    continue
                non_rate_limit_attempts += 1
                if non_rate_limit_attempts < retries:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)
                else:
                    raise
            except (aiohttp.ClientError, asyncio.TimeoutError):
                non_rate_limit_attempts += 1
                if non_rate_limit_attempts < retries:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)
                else:
                    raise
        raise RuntimeError("Request loop exited without returning")  # unreachable

    async def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request("POST", path, json=body or {})

    async def patch(
        self,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request("PATCH", path, json=body or {})

    async def get_all_pages(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Fetch all pages of a paginated endpoint, returning a flat list of items."""
        results: list[dict[str, Any]] = []
        page = 1
        base_params = dict(params or {})
        base_params.setdefault("limit", 20)
        while True:
            base_params["page"] = page
            raw = await self._get_raw(path, params=base_params)
            items = raw.get("data", [])
            if not isinstance(items, list) or not items:
                break
            results.extend(items)
            total = raw.get("meta", {}).get("total", len(results))
            if len(results) >= total:
                break
            page += 1
        return results

    async def pages(
        self, path: str, params: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Async generator yielding individual items across all pages."""
        for item in await self.get_all_pages(path, params):
            yield item

    # ------------------------------------------------------------------
    # Token helpers
    # ------------------------------------------------------------------

    def save_token(self, token: str, symbol: str) -> None:
        """Persist token and agent symbol to the shared .env file."""
        env_path = str(_ENV_FILE)
        if not _ENV_FILE.exists():
            _ENV_FILE.write_text("")
        set_key(env_path, "SPACETRADERS_TOKEN", token)
        set_key(env_path, "AGENT_SYMBOL", symbol)
        self._token = token
        # Refresh session headers if already open
        if self._session:
            self._session.headers.update(self._make_headers())
        load_dotenv(dotenv_path=_ENV_FILE, override=True)


def save_token(token: str, symbol: str) -> None:
    """Module-level helper: persist token and agent symbol to the shared .env file."""
    env_path = str(_ENV_FILE)
    if not _ENV_FILE.exists():
        _ENV_FILE.write_text("")
    set_key(env_path, "SPACETRADERS_TOKEN", token)
    set_key(env_path, "AGENT_SYMBOL", symbol)
    load_dotenv(dotenv_path=_ENV_FILE, override=True)
