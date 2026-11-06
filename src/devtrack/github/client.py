"""GitHub REST API client: auth, pagination, rate limits, retries with backoff, logging.

Design points
  * The token comes from the environment (GITHUB_TOKEN) via Settings; it is sent as a
    header and never logged (the log filter in logging_setup is a second line of defence).
  * Pagination follows the ``Link: <...>; rel="next"`` header instead of computing page
    numbers, which is what GitHub documents and survives cursor-based endpoints.
  * Rate limits: a 403/429 with ``X-RateLimit-Remaining: 0`` waits until
    ``X-RateLimit-Reset``; a ``Retry-After`` header (secondary limits) is honoured; and
    when the remaining budget gets low we slow down before hitting the wall.
  * Retries: 5xx and connection errors are retried with exponential backoff and full
    jitter. Other 4xx errors are not retried; they mean the request itself is wrong.
  * Time and sleeping are injected so tests run instantly and deterministically.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
_LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubError(RuntimeError):
    def __init__(self, status: int, url: str, message: str) -> None:
        super().__init__(f"GitHub {status} for {url}: {message}")
        self.status = status
        self.url = url


class RateLimitExhausted(GitHubError):
    """Raised when waiting for the reset would exceed ``max_rate_limit_wait``."""


class HTTPSession(Protocol):
    """The slice of ``requests.Session`` the client uses (lets tests pass a fake)."""

    headers: Any

    def get(self, url: str, *, params: dict[str, Any] | None = ..., timeout: float = ...) -> Any: ...


@dataclass
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 1.0  # seconds; attempt n waits up to base * 2**n
    max_delay: float = 60.0
    max_rate_limit_wait: float = 15 * 60  # don't sleep longer than this for a reset
    low_water: int = 50  # start pacing requests when this few remain


@dataclass
class GitHubClient:
    token: str | None = field(default=None, repr=False)
    session: HTTPSession | None = None
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    timeout: float = 30.0
    per_page: int = 100
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    rng: random.Random = field(default_factory=random.Random)
    requests_made: int = 0

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "devtrack-compiler",
            }
        )
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"
        else:
            log.warning("GITHUB_TOKEN not set: unauthenticated requests are limited to 60 per hour")

    # -- public API ----------------------------------------------------------

    def get_json(self, path_or_url: str, params: dict[str, Any] | None = None) -> Any:
        response = self._request(self._url(path_or_url), params)
        return response.json()

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
        stop: Callable[[dict[str, Any]], bool] | None = None,
        items_key: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield items across pages. ``stop(item)`` returning True ends the walk early
        (used for "sorted by updated, stop when older than the watermark").
        ``items_key`` is for endpoints that wrap the list in an object, such as
        ``{"total_count": .., "workflow_runs": [..]}`` from the Actions API."""
        url: str | None = self._url(path)
        query: dict[str, Any] | None = {"per_page": self.per_page, **(params or {})}
        seen = 0
        while url:
            response = self._request(url, query)
            items = response.json()
            if items_key is not None and isinstance(items, dict):
                items = items.get(items_key)
            if not isinstance(items, list):
                raise GitHubError(response.status_code, url, "expected a JSON list from a paginated endpoint")
            for item in items:
                if stop is not None and stop(item):
                    return
                yield item
                seen += 1
                if max_items is not None and seen >= max_items:
                    return
            url = self._next_link(response.headers.get("Link", ""))
            query = None  # the next link already carries the query string

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _url(path_or_url: str) -> str:
        return path_or_url if path_or_url.startswith("http") else f"{API_ROOT}/{path_or_url.lstrip('/')}"

    @staticmethod
    def _next_link(header: str) -> str | None:
        match = _LINK_NEXT.search(header or "")
        return match.group(1) if match else None

    def _backoff(self, attempt: int) -> float:
        # Full jitter: uniform(0, min(cap, base * 2**attempt)). Spreads retries out so
        # many workers don't hammer the API in lockstep.
        ceiling = min(self.policy.max_delay, self.policy.base_delay * (2**attempt))
        return self.rng.uniform(0, ceiling)

    def _rate_limit_wait(self, response: Any) -> float | None:
        """Seconds to wait if this response is a rate-limit response, else None."""
        if response.status_code not in (403, 429):
            return None
        headers = response.headers
        retry_after = headers.get("Retry-After")
        if retry_after is not None:
            return max(float(retry_after), 1.0)
        if headers.get("X-RateLimit-Remaining") == "0" and headers.get("X-RateLimit-Reset"):
            return max(float(headers["X-RateLimit-Reset"]) - self.clock(), 0) + 1.0
        if response.status_code == 429:
            return None  # 429 without hints: fall back to exponential backoff
        return None  # a plain 403 is a permissions problem, not a rate limit

    def _pace(self, response: Any) -> None:
        """Slow down when the remaining budget is low, before GitHub makes us stop."""
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset = response.headers.get("X-RateLimit-Reset")
        if remaining is None or reset is None:
            return
        left = int(remaining)
        if 0 < left <= self.policy.low_water:
            window = max(float(reset) - self.clock(), 0)
            delay = window / left
            if delay > 0.05:
                log.info("rate limit low (%s left); pacing %.1fs per request", left, delay)
                self.sleep(min(delay, self.policy.max_delay))

    def _request(self, url: str, params: dict[str, Any] | None) -> Any:
        assert self.session is not None
        last_error: str = ""
        for attempt in range(self.policy.max_attempts):
            try:
                self.requests_made += 1
                response = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = type(exc).__name__
                delay = self._backoff(attempt)
                log.warning("GET %s failed (%s); retry %d in %.1fs", url, last_error, attempt + 1, delay)
                self.sleep(delay)
                continue

            status = response.status_code
            if 200 <= status < 300:
                log.debug("GET %s -> %s", url, status)
                self._pace(response)
                return response

            wait = self._rate_limit_wait(response)
            if wait is not None:
                if wait > self.policy.max_rate_limit_wait:
                    raise RateLimitExhausted(status, url, f"reset in {wait:.0f}s exceeds the wait limit")
                log.warning("rate limited on %s; sleeping %.0fs", url, wait)
                self.sleep(wait)
                continue

            if status >= 500 or status == 429:
                last_error = f"HTTP {status}"
                delay = self._backoff(attempt)
                log.warning("GET %s -> %s; retry %d in %.1fs", url, status, attempt + 1, delay)
                self.sleep(delay)
                continue

            message = _error_message(response)
            log.error("GET %s -> %s: %s", url, status, message)
            raise GitHubError(status, url, message)

        raise GitHubError(0, url, f"gave up after {self.policy.max_attempts} attempts ({last_error})")


def _error_message(response: Any) -> str:
    try:
        body = response.json()
        return str(body.get("message", body)) if isinstance(body, dict) else str(body)[:200]
    except ValueError:
        return str(getattr(response, "text", ""))[:200]
