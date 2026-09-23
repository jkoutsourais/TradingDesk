import asyncio

import httpx

from desk.collectors.base import RateLimiter, describe_http_error


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_rate_limiter_waits_for_window_to_free() -> None:
    clock = FakeClock()
    limiter = RateLimiter(3, 60.0, clock=clock, sleep=clock.sleep)

    async def scenario() -> None:
        for _ in range(3):
            await limiter.acquire()
        assert clock.sleeps == []
        await limiter.acquire()

    asyncio.run(scenario())
    assert clock.sleeps == [60.0]


def test_rate_limiter_allows_after_period_elapses() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2, 1.0, clock=clock, sleep=clock.sleep)

    async def scenario() -> None:
        await limiter.acquire()
        clock.now = 0.5
        await limiter.acquire()
        clock.now = 1.0
        await limiter.acquire()  # the first call has aged out

    asyncio.run(scenario())
    assert clock.sleeps == []


def test_describe_http_error_drops_query_string() -> None:
    request = httpx.Request("GET", "https://api.example.com/series?api_key=SECRET123&id=DGS10")
    response = httpx.Response(429, request=request)
    error = httpx.HTTPStatusError("too many", request=request, response=response)
    text = describe_http_error(error)
    assert "SECRET123" not in text
    assert text == "GET https://api.example.com/series -> HTTP 429"


def test_describe_transport_error_drops_query_string() -> None:
    request = httpx.Request("GET", "https://api.example.com/x?api_key=SECRET123")
    error = httpx.ConnectTimeout("timed out", request=request)
    assert "SECRET123" not in describe_http_error(error)
