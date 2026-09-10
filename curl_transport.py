"""httpx AsyncHTTPTransport backed by curl_cffi for browser TLS impersonation.

Standard httpx/python TLS fingerprints get Cloudflare-challenged (403) on
datacenter IPs. This transport routes every request through curl_cffi with a
Chrome fingerprint so api.x.com treats the traffic like a real browser.
"""

import httpx
from curl_cffi.requests import AsyncSession


class CurlCffiTransport(httpx.AsyncBaseTransport):
    def __init__(
        self, impersonate: str = "chrome", fresh_session_per_request: bool = False
    ) -> None:
        self._impersonate = impersonate
        self._fresh = fresh_session_per_request
        self._session = AsyncSession(impersonate=impersonate)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Single source of truth for cookies is the httpx client jar (sent as
        # the Cookie header). Clear curl's internal jar every request so stale
        # homepage/guest cookies never leak into api.x.com calls and trigger
        # Cloudflare challenges.
        body = await request.aread()
        if self._fresh:
            session = AsyncSession(impersonate=self._impersonate)
            try:
                resp = await session.request(
                    method=request.method,
                    url=str(request.url),
                    headers=dict(request.headers),
                    content=body or None,
                    timeout=60,
                )
                content, status, headers = resp.content, resp.status_code, dict(
                    resp.headers
                )
            finally:
                await session.close()
        else:
            self._session.cookies.clear()
            resp = await self._session.request(
                method=request.method,
                url=str(request.url),
                headers=dict(request.headers),
                content=body or None,
                timeout=60,
            )
            content, status, headers = resp.content, resp.status_code, dict(
                resp.headers
            )
        # curl_cffi already decompresses the body — strip the encoding header
        # so httpx doesn't try to decode it a second time.
        headers = {k: v for k, v in headers.items() if k.lower() != "content-encoding"}
        return httpx.Response(
            status_code=status,
            headers=headers,
            content=content,
            request=request,
        )

    async def aclose(self) -> None:
        await self._session.close()
