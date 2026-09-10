"""httpx AsyncHTTPTransport backed by curl_cffi for browser TLS impersonation.

Standard httpx/python TLS fingerprints get Cloudflare-challenged (403) on
datacenter IPs. This transport routes every request through curl_cffi with a
Chrome fingerprint so api.x.com treats the traffic like a real browser.
"""

import httpx
from curl_cffi.requests import AsyncSession


class CurlCffiTransport(httpx.AsyncBaseTransport):
    def __init__(self, impersonate: str = "chrome") -> None:
        self._session = AsyncSession(impersonate=impersonate)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Single source of truth for cookies is the httpx client jar (sent as
        # the Cookie header). Clear curl's internal jar every request so stale
        # homepage/guest cookies never leak into api.x.com calls and trigger
        # Cloudflare challenges.
        self._session.cookies.clear()
        body = await request.aread()
        resp = await self._session.request(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            content=body or None,
            timeout=60,
        )
        # curl_cffi already decompresses the body — strip the encoding header
        # so httpx doesn't try to decode it a second time.
        headers = {
            k: v for k, v in resp.headers.items() if k.lower() != "content-encoding"
        }
        return httpx.Response(
            status_code=resp.status_code,
            headers=headers,
            content=resp.content,
            request=request,
        )

    async def aclose(self) -> None:
        await self._session.close()
