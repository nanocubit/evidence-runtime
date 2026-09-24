"""L3 browser acquisition via Playwright (optional dependency).

Reuses a single Chromium process; each fetch gets an isolated browser context.
Does not parse fields — returns HTML for extract_l1.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from .policies import BrowserPolicy, PolicyError

logger = logging.getLogger(__name__)


@dataclass
class BrowserDocument:
    url: str
    text: str
    status_code: int
    content_hash: str
    content_signature: str
    headers: dict[str, str]
    ttfb_ms: float | None = None
    body: bytes = b""
    backend: str = "L3_playwright"
    from_pool: bool = False


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


class BrowserPool:
    """
    Process-level Playwright pool.

    - One Chromium browser process (expensive to launch)
    - Semaphore-limited concurrent pages
    - Fresh BrowserContext per fetch (cookie / storage isolation)
    """

    def __init__(
        self,
        *,
        max_concurrency: int = 3,
        headless: bool = True,
    ) -> None:
        self.max_concurrency = max(1, max_concurrency)
        self.headless = headless
        self._sem = asyncio.Semaphore(self.max_concurrency)
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self._started = False
        self._launches = 0
        self._fetches = 0
        self._errors = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "launches": self._launches,
            "fetches": self._fetches,
            "errors": self._errors,
            "max_concurrency": self.max_concurrency,
            "started": int(self._started),
        }

    async def start(self, headless: bool | None = None) -> None:
        if headless is not None:
            self.headless = headless
        async with self._lock:
            if self._started and self._browser is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:
                raise PolicyError(
                    "playwright_not_installed: pip install playwright && playwright install chromium"
                ) from e
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self.headless)
            self._launches += 1
            self._started = True
            logger.info("BrowserPool started (launch #%s)", self._launches)

    async def stop(self) -> None:
        async with self._lock:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            self._started = False
            logger.info("BrowserPool stopped")

    async def _ensure_browser(self, policy: BrowserPolicy) -> Any:
        if not self._started or self._browser is None:
            await self.start(headless=policy.headless)
            return self._browser
        # Recover if browser disconnected
        try:
            if not self._browser.is_connected():
                logger.warning("BrowserPool: browser disconnected, relaunching")
                await self.stop()
                await self.start(headless=policy.headless)
        except Exception:
            await self.stop()
            await self.start(headless=policy.headless)
        return self._browser

    async def fetch(
        self,
        url: str,
        policy: BrowserPolicy | None = None,
        *,
        user_agent: str = "evidence-runtime/0.3 (+research; L3)",
    ) -> BrowserDocument:
        policy = policy or BrowserPolicy()
        if not policy.enabled:
            raise PolicyError("browser_disabled")

        await self._sem.acquire()
        t0 = perf_counter()
        context = None
        try:
            browser = await self._ensure_browser(policy)
            ua = policy.user_agent or user_agent
            context = await browser.new_context(
                user_agent=ua,
                viewport={
                    "width": policy.viewport_width,
                    "height": policy.viewport_height,
                },
            )
            page = await context.new_page()

            async def _route(route: Any) -> None:
                if route.request.resource_type in policy.block_resources:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", _route)

            timeout_ms = int(policy.timeout_s * 1000)
            response = await page.goto(
                url,
                wait_until=policy.wait_until,  # type: ignore[arg-type]
                timeout=timeout_ms,
            )
            status = response.status if response is not None else 0
            if policy.wait_selector:
                try:
                    await page.wait_for_selector(
                        policy.wait_selector,
                        timeout=policy.wait_selector_timeout_ms,
                    )
                except Exception:
                    pass

            html = await page.content()
            final_url = page.url
            self._fetches += 1
        except Exception:
            self._errors += 1
            raise
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            self._sem.release()

        ttfb_ms = (perf_counter() - t0) * 1000
        body = (html or "").encode("utf-8", errors="replace")
        content_hash = "sha256:" + hashlib.sha256(body).hexdigest()
        content_signature = content_hash[7:23]

        if not html or len(html.strip()) < 20:
            raise PolicyError("no_html:empty_body_browser")

        return BrowserDocument(
            url=final_url,
            text=html,
            status_code=status or 200,
            content_hash=content_hash,
            content_signature=content_signature,
            headers={},
            ttfb_ms=ttfb_ms,
            body=body,
            from_pool=True,
        )


# Process-wide singleton
_pool: BrowserPool | None = None
_pool_lock = asyncio.Lock()


def get_browser_pool(
    *,
    max_concurrency: int | None = None,
    headless: bool = True,
) -> BrowserPool:
    global _pool
    if _pool is None:
        _pool = BrowserPool(
            max_concurrency=max_concurrency or 3,
            headless=headless,
        )
    return _pool


async def shutdown_browser_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.stop()
        _pool = None


async def fetch_browser(
    url: str,
    policy: BrowserPolicy | None = None,
    *,
    user_agent: str = "evidence-runtime/0.3 (+research; L3)",
    use_pool: bool = True,
) -> BrowserDocument:
    """Navigate with headless Chromium and return page HTML.

    By default uses the process-wide BrowserPool (reuse Chromium).
    Set use_pool=False for a one-shot launch (tests / isolation).
    """
    policy = policy or BrowserPolicy()
    if not policy.enabled:
        raise PolicyError("browser_disabled")

    if use_pool and getattr(policy, "use_pool", True):
        pool = get_browser_pool(
            max_concurrency=getattr(policy, "pool_concurrency", 3) or 3,
            headless=policy.headless,
        )
        return await pool.fetch(url, policy, user_agent=user_agent)

    # One-shot path (no reuse)
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise PolicyError(
            "playwright_not_installed: pip install playwright && playwright install chromium"
        ) from e

    ua = policy.user_agent or user_agent
    t0 = perf_counter()
    html = ""
    final_url = url
    status = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=policy.headless)
        try:
            context = await browser.new_context(
                user_agent=ua,
                viewport={
                    "width": policy.viewport_width,
                    "height": policy.viewport_height,
                },
            )
            page = await context.new_page()

            async def _route(route: Any) -> None:
                if route.request.resource_type in policy.block_resources:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", _route)
            timeout_ms = int(policy.timeout_s * 1000)
            response = await page.goto(
                url,
                wait_until=policy.wait_until,  # type: ignore[arg-type]
                timeout=timeout_ms,
            )
            if response is not None:
                status = response.status
            if policy.wait_selector:
                try:
                    await page.wait_for_selector(
                        policy.wait_selector,
                        timeout=policy.wait_selector_timeout_ms,
                    )
                except Exception:
                    pass
            html = await page.content()
            final_url = page.url
            await context.close()
        finally:
            await browser.close()

    ttfb_ms = (perf_counter() - t0) * 1000
    body = html.encode("utf-8", errors="replace")
    content_hash = "sha256:" + hashlib.sha256(body).hexdigest()
    content_signature = content_hash[7:23]
    if not html or len(html.strip()) < 20:
        raise PolicyError("no_html:empty_body_browser")

    return BrowserDocument(
        url=final_url,
        text=html,
        status_code=status or 200,
        content_hash=content_hash,
        content_signature=content_signature,
        headers={},
        ttfb_ms=ttfb_ms,
        body=body,
        from_pool=False,
    )
