"""Phase 2: the API client handles auth, pagination, rate limits and retries."""

from __future__ import annotations

import logging
import random

import pytest

from devtrack.github.client import GitHubClient, GitHubError, RateLimitExhausted, RetryPolicy
from devtrack.logging_setup import RedactingFilter, redact
from tests.fakes import FakeResponse, ScriptedSession, connection_error

TOKEN = "ghp_" + "A" * 36  # shape of a real token; obviously fake


def make(
    responses: list[FakeResponse | Exception], **kw: object
) -> tuple[GitHubClient, ScriptedSession, list[float]]:
    session = ScriptedSession(responses)
    sleeps: list[float] = []
    client = GitHubClient(
        token=TOKEN,
        session=session,
        sleep=sleeps.append,
        clock=lambda: 1_000.0,
        rng=random.Random(0),
        **kw,  # type: ignore[arg-type]
    )
    return client, session, sleeps


def ok(body: object, headers: dict[str, str] | None = None) -> FakeResponse:
    return FakeResponse(200, body, headers or {})


def test_token_sent_as_bearer_header() -> None:
    client, session, _ = make([ok({})])
    client.get_json("rate_limit")
    assert session.headers["Authorization"] == f"Bearer {TOKEN}"
    assert session.headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_token_never_appears_in_repr_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    client, _, _ = make([FakeResponse(404, {"message": "Not Found"})])
    assert TOKEN not in repr(client)
    logging.getLogger().addFilter(RedactingFilter())
    with caplog.at_level(logging.DEBUG), pytest.raises(GitHubError):
        client.get_json("repos/x/y")
    logging.getLogger("devtrack").info("debug dump: Authorization: Bearer %s", TOKEN)
    assert TOKEN not in caplog.text
    assert redact(f"token={TOKEN}") == "token=[REDACTED]"


def test_pagination_follows_link_header() -> None:
    client, session, _ = make(
        [
            ok([{"n": 1}, {"n": 2}], {"Link": '<https://api.github.com/x?page=2>; rel="next"'}),
            ok([{"n": 3}], {"Link": '<https://api.github.com/x?page=1>; rel="prev"'}),
        ]
    )
    assert [i["n"] for i in client.paginate("x")] == [1, 2, 3]
    assert session.calls[0][1] == {"per_page": 100}
    assert session.calls[1] == ("https://api.github.com/x?page=2", None)  # next link carries its own query


def test_pagination_respects_max_items_and_stop() -> None:
    client, _, _ = make([ok([{"n": 1}, {"n": 2}, {"n": 3}])])
    assert [i["n"] for i in client.paginate("x", max_items=2)] == [1, 2]
    client, _, _ = make([ok([{"n": 1}, {"n": 2}, {"n": 3}])])
    assert [i["n"] for i in client.paginate("x", stop=lambda i: i["n"] == 2)] == [1]


def test_server_errors_are_retried_with_backoff() -> None:
    client, session, sleeps = make([FakeResponse(502), FakeResponse(503), ok({"done": True})])
    assert client.get_json("x") == {"done": True}
    assert len(session.calls) == 3
    assert len(sleeps) == 2
    assert 0 <= sleeps[0] <= 1.0 and 0 <= sleeps[1] <= 2.0  # full jitter under the exponential ceiling


def test_connection_errors_are_retried() -> None:
    client, _, sleeps = make([connection_error(), ok([])])
    assert client.get_json("x") == []
    assert len(sleeps) == 1


def test_gives_up_after_max_attempts() -> None:
    client, session, _ = make([FakeResponse(500)] * 3, policy=RetryPolicy(max_attempts=3))
    with pytest.raises(GitHubError, match="gave up after 3 attempts"):
        client.get_json("x")
    assert len(session.calls) == 3


def test_primary_rate_limit_waits_for_reset() -> None:
    limited = FakeResponse(
        403,
        {"message": "API rate limit exceeded"},
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1060"},
    )
    client, _, sleeps = make([limited, ok({"ok": 1})])
    assert client.get_json("x") == {"ok": 1}
    assert sleeps == [61.0]  # reset (1060) - now (1000) + 1s margin


def test_secondary_rate_limit_honours_retry_after() -> None:
    client, _, sleeps = make([FakeResponse(429, {}, {"Retry-After": "7"}), ok({})])
    client.get_json("x")
    assert sleeps == [7.0]


def test_rate_limit_too_far_away_raises() -> None:
    limited = FakeResponse(403, {}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "999999"})
    client, _, _ = make([limited])
    with pytest.raises(RateLimitExhausted):
        client.get_json("x")


def test_plain_403_and_404_are_not_retried() -> None:
    client, session, _ = make([FakeResponse(403, {"message": "Resource not accessible"})])
    with pytest.raises(GitHubError, match="Resource not accessible"):
        client.get_json("x")
    assert len(session.calls) == 1
    client, session, _ = make([FakeResponse(404, {"message": "Not Found"})])
    with pytest.raises(GitHubError) as err:
        client.get_json("x")
    assert err.value.status == 404 and len(session.calls) == 1


def test_paces_requests_when_budget_is_low() -> None:
    client, _, sleeps = make([ok({}, {"X-RateLimit-Remaining": "10", "X-RateLimit-Reset": "1100"})])
    client.get_json("x")
    assert sleeps == [10.0]  # 100s left in the window / 10 requests left


def test_unauthenticated_client_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        GitHubClient(token=None, session=ScriptedSession([]))
    assert "60 per hour" in caplog.text
