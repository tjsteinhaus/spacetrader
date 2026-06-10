"""
SpaceTraders API base client.
Handles auth, request retries, rate limit handling, and error formatting.
"""
import os
import time
import requests
from dotenv import load_dotenv, set_key

load_dotenv()

BASE_URL = "https://api.spacetraders.io/v2"
ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")


class SpaceTradersError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def _get_token() -> str | None:
    return os.getenv("SPACETRADERS_TOKEN") or None


def _headers(token: str | None = None) -> dict:
    t = token or _get_token()
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if t:
        h["Authorization"] = f"Bearer {t}"
    return h


def _handle_response(resp: requests.Response) -> dict:
    if resp.status_code == 429:
        # Rate limited – respect Retry-After header
        retry_after = float(resp.headers.get("Retry-After", 1))
        time.sleep(retry_after)
        raise SpaceTradersError(429, "Rate limited – retried after backoff")
    try:
        data = resp.json()
    except Exception:
        resp.raise_for_status()
        return {}

    if resp.ok:
        return data.get("data", data)

    error = data.get("error", {})
    raise SpaceTradersError(
        error.get("code", resp.status_code),
        error.get("message", resp.text),
    )


def _request_with_retry(method: str, path: str, *, params=None, json=None, token=None, retries=5) -> dict:
    delay = 5
    non_rate_limit_attempts = 0
    while non_rate_limit_attempts < retries:
        try:
            resp = requests.request(
                method,
                f"{BASE_URL}{path}",
                headers=_headers(token),
                params=params,
                json=json,
                timeout=30,
            )
            return _handle_response(resp)
        except SpaceTradersError as e:
            if e.code == 429:
                # _handle_response already slept for Retry-After; retry indefinitely
                continue
            non_rate_limit_attempts += 1
            if non_rate_limit_attempts < retries:
                time.sleep(delay)
                delay = min(delay * 2, 60)
            else:
                raise
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            non_rate_limit_attempts += 1
            if non_rate_limit_attempts < retries:
                time.sleep(delay)
                delay = min(delay * 2, 60)
            else:
                raise


def get(path: str, params: dict | None = None, token: str | None = None) -> dict:
    return _request_with_retry("GET", path, params=params, token=token)


def get_raw(path: str, params: dict | None = None, token: str | None = None) -> dict:
    """Like get() but returns the full response dict including 'meta' and 'data'."""
    t = token or _get_token()
    delay = 1
    for attempt in range(10):
        resp = requests.get(
            f"{BASE_URL}{path}",
            headers=_headers(t),
            params=params,
            timeout=30,
        )
        if resp.status_code == 429:
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return resp.json()


def post(path: str, body: dict | None = None, token: str | None = None) -> dict:
    return _request_with_retry("POST", path, json=body or {}, token=token)


def patch(path: str, body: dict | None = None, token: str | None = None) -> dict:
    return _request_with_retry("PATCH", path, json=body or {}, token=token)


def save_token(token: str, symbol: str) -> None:
    """Persist token and agent symbol to .env file."""
    if not os.path.exists(ENV_FILE):
        with open(ENV_FILE, "w") as f:
            f.write("")
    set_key(ENV_FILE, "SPACETRADERS_TOKEN", token)
    set_key(ENV_FILE, "AGENT_SYMBOL", symbol)
    # Reload so current process picks it up
    load_dotenv(override=True)


def get_all_pages(path: str, token: str | None = None) -> list:
    """Fetch all pages of a paginated endpoint."""
    results = []
    page = 1
    while True:
        raw = get_raw(path, params={"page": page, "limit": 20}, token=token)
        items = raw.get("data", [])
        if isinstance(items, list):
            results.extend(items)
            total = raw.get("meta", {}).get("total", len(results))
            if len(results) >= total:
                break
            page += 1
        else:
            break
    return results
