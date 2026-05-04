"""
Arbitrage-X Ingestion — Shared HTTP utilities
RetryClient: exponential backoff + Retry-After header support
SPAPIAuth:   Amazon LWA token manager with auto-refresh
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx

from config.settings import (
    SP_API_LWA_APP_ID,
    SP_API_LWA_CLIENT_SECRET,
    SP_API_REFRESH_TOKEN,
)

logger = logging.getLogger(__name__)

# Status codes that warrant a retry
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Network-level exceptions that warrant a retry
_RETRY_EXCEPTIONS = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
)

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


class RetryClient:
    """
    httpx.Client wrapper with exponential-backoff retry.

    Retry policy:
      - HTTP 429/5xx → exponential backoff; respects Retry-After header
      - Network errors (timeout, connect) → same backoff
      - Non-retryable HTTP errors → raise immediately
    """

    def __init__(
        self,
        *,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        headers: Optional[dict] = None,
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._http = httpx.Client(timeout=timeout, headers=headers or {})

    def _backoff(self, attempt: int, retry_after: Optional[float] = None) -> float:
        if retry_after is not None:
            return min(retry_after, self.max_delay)
        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
        # Add up to 25% jitter to spread concurrent retries
        return delay + random.uniform(0, delay * 0.25)

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.request(method, url, **kwargs)

                if resp.status_code not in _RETRY_STATUSES:
                    resp.raise_for_status()
                    return resp

                # Parse Retry-After if present
                retry_after: Optional[float] = None
                try:
                    retry_after = float(resp.headers.get("Retry-After", ""))
                except (ValueError, TypeError):
                    pass

                if attempt == self.max_retries:
                    resp.raise_for_status()  # raises HTTPStatusError

                delay = self._backoff(attempt, retry_after)
                logger.warning(
                    "HTTP %d — %s %s  retry %d/%d in %.1fs",
                    resp.status_code, method, url, attempt + 1, self.max_retries, delay,
                )
                time.sleep(delay)

            except _RETRY_EXCEPTIONS as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    raise
                delay = self._backoff(attempt)
                logger.warning(
                    "%s — %s %s  retry %d/%d in %.1fs",
                    type(exc).__name__, method, url, attempt + 1, self.max_retries, delay,
                )
                time.sleep(delay)

        raise RuntimeError(f"Exhausted {self.max_retries} retries for {method} {url}") from last_exc

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "RetryClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()


class SPAPIAuth:
    """
    Amazon LWA (Login with Amazon) token manager.
    Caches the access token until 60s before expiry.
    """

    LWA_URL = "https://api.amazon.com/auth/o2/token"

    def __init__(self):
        self._token: Optional[str] = None
        self._expires_at: Optional[datetime] = None
        self._http = httpx.Client(timeout=httpx.Timeout(10.0))

    def get_token(self) -> str:
        now = datetime.utcnow()
        if self._token and self._expires_at and now < self._expires_at:
            return self._token

        resp = self._http.post(
            self.LWA_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": SP_API_REFRESH_TOKEN,
                "client_id": SP_API_LWA_APP_ID,
                "client_secret": SP_API_LWA_CLIENT_SECRET,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        ttl = int(data.get("expires_in", 3600))
        self._expires_at = now + timedelta(seconds=ttl - 60)
        logger.debug("LWA token refreshed, expires in %ds", ttl)
        return self._token

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SPAPIAuth":
        return self

    def __exit__(self, *_) -> None:
        self.close()
