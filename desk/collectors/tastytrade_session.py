"""One shared tastytrade API session for the stream and the metrics collector.

The SDK refreshes the 15-minute access token before each request, so a single session
lives for the whole process. It is opened lazily on first use and closed at shutdown.
"""

import asyncio
from contextlib import AsyncExitStack

from tastytrade import Session


class TastytradeConnection:
    def __init__(self, client_secret: str, refresh_token: str) -> None:
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._stack: AsyncExitStack | None = None
        self._session: Session | None = None
        self._lock = asyncio.Lock()

    async def session(self) -> Session:
        async with self._lock:
            if self._session is None:
                stack = AsyncExitStack()
                self._session = await stack.enter_async_context(
                    Session(provider_secret=self._client_secret, refresh_token=self._refresh_token)
                )
                self._stack = stack
            return self._session

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None
