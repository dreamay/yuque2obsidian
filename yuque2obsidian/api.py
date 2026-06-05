"""Async Yuque API client with rate limiting and retry."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from yuque2obsidian.models import DocDetail, DocSummary, Group, Repo, TocNode, User

logger = logging.getLogger("yuque2obsidian")


class RateLimiter:
    """Simple token-bucket-like rate limiter using asyncio."""

    def __init__(self, rate: float) -> None:
        """rate: maximum requests per second."""
        self.rate = rate
        self.min_interval = 1.0 / rate
        self._last_release = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait_time = max(0.0, self._last_release + self.min_interval - now)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
                now = asyncio.get_event_loop().time()
            self._last_release = now


class YuqueAPI:
    """Async client for Yuque API v2."""

    def __init__(
        self,
        token: str,
        base_url: str = "https://www.yuque.com/api/v2",
        concurrency: int = 5,
        rate_limit: float = 1.3,
        timeout: float = 60.0,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "X-Auth-Token": token,
            "Content-Type": "application/json",
        }
        self.client = httpx.AsyncClient(
            headers=self.headers,
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
        )
        self.semaphore = asyncio.Semaphore(concurrency)
        self.rate_limiter = RateLimiter(rate_limit)

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        async with self.semaphore:
            await self.rate_limiter.acquire()
            response = await self.client.request(
                method, url, params=params, json=json_body
            )
            response.raise_for_status()
            payload = response.json()
            return payload.get("data")

    # Expose a retry-wrapped request helper for call sites that want it.
    @retry(
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.NetworkError, httpx.TimeoutException)),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=60),
        reraise=True,
    )
    async def request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        try:
            return await self._request(method, path, params, json_body)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning("Rate limited by Yuque, backing off...")
                raise  # trigger retry
            if exc.response.status_code >= 500:
                logger.warning("Server error %s, retrying...", exc.response.status_code)
                raise  # trigger retry
            logger.error("HTTP error %s on %s: %s", exc.response.status_code, path, exc.response.text)
            raise
        except (httpx.NetworkError, httpx.TimeoutException) as exc:
            logger.warning("Network/timeout error on %s: %s", path, exc)
            raise

    async def get_user(self) -> User:
        data = await self.request("GET", "/user")
        return User.model_validate(data)

    async def list_user_repos(self, login: str) -> list[Repo]:
        repos: list[Repo] = []
        offset = 0
        limit = 20
        while True:
            data = await self.request(
                "GET",
                f"/users/{login}/repos",
                params={"offset": offset, "limit": limit},
            )
            if not data:
                break
            repos.extend(Repo.model_validate(item) for item in data)
            if len(data) < limit:
                break
            offset += limit
        return repos

    async def list_user_groups(self, login: str) -> list[Group]:
        data = await self.request("GET", f"/users/{login}/groups")
        if not data:
            return []
        return [Group.model_validate(item) for item in data]

    async def list_group_repos(self, group_login: str) -> list[Repo]:
        repos: list[Repo] = []
        offset = 0
        limit = 20
        while True:
            data = await self.request(
                "GET",
                f"/groups/{group_login}/repos",
                params={"offset": offset, "limit": limit},
            )
            if not data:
                break
            repos.extend(Repo.model_validate(item) for item in data)
            if len(data) < limit:
                break
            offset += limit
        return repos

    async def get_repo_toc(self, namespace: str) -> list[TocNode]:
        data = await self.request("GET", f"/repos/{namespace}/toc")
        if not data:
            return []
        return [TocNode.model_validate(item) for item in data]

    async def list_repo_docs(self, namespace: str) -> list[DocSummary]:
        docs: list[DocSummary] = []
        offset = 0
        limit = 20
        while True:
            data = await self.request(
                "GET",
                f"/repos/{namespace}/docs",
                params={"offset": offset, "limit": limit},
            )
            if not data:
                break
            docs.extend(DocSummary.model_validate(item) for item in data)
            if len(data) < limit:
                break
            offset += limit
        return docs

    async def get_doc_detail(self, namespace: str, slug: str) -> DocDetail:
        data = await self.request("GET", f"/repos/{namespace}/docs/{slug}")
        return DocDetail.model_validate(data)

    # ------------------------------------------------------------------
    # Unofficial web API fallback (for Lake → Markdown server conversion)
    # ------------------------------------------------------------------

    async def get_doc_markdown_via_web_api(
        self, slug: str, book_id: int
    ) -> Optional[str]:
        """Try to fetch pre-converted Markdown from the unofficial web API.

        The unofficial endpoint ``/api/docs/{slug}?book_id={id}&mode=markdown``
        asks Yuque to convert the document (including Lake format) to Markdown
        on the server side.  This often produces cleaner output than local
        HTML→Markdown conversion.

        Authentication: this endpoint historically requires a web session
        (cookie).  The Token used for API v2 may or may not work here, so we
        try gracefully and return *None* on any failure so the caller can
        fall back to the official ``body`` / ``body_html`` fields.
        """
        url = f"https://www.yuque.com/api/docs/{slug}"
        params = {
            "book_id": str(book_id),
            "merge_dynamic_data": "false",
            "mode": "markdown",
        }
        async with self.semaphore:
            await self.rate_limiter.acquire()
            try:
                response = await self.client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data", {})
                # The server-converted Markdown lives in ``sourcecode``.
                source = data.get("sourcecode")
                if isinstance(source, str) and source.strip():
                    logger.debug(
                        "Web API markdown fallback succeeded for doc %s (book_id=%s)",
                        slug,
                        book_id,
                    )
                    return source
            except httpx.HTTPStatusError as exc:
                # 401/403 means the Token doesn't work for the web API.
                if exc.response.status_code in (401, 403):
                    logger.debug(
                        "Web API markdown fallback auth failed for doc %s: %s",
                        slug,
                        exc.response.status_code,
                    )
                else:
                    logger.debug(
                        "Web API markdown fallback HTTP %s for doc %s",
                        exc.response.status_code,
                        slug,
                    )
            except Exception as exc:
                logger.debug(
                    "Web API markdown fallback failed for doc %s: %s",
                    slug,
                    exc,
                )
        return None
