"""Real provider SDK clients, env-gated behind ``ProviderMode.LIVE`` (dra#26).

Each client implements the same Protocol as its deterministic fake counterpart
in :mod:`dra.routing.providers` (``SearchProvider`` / ``SiteMapProvider`` /
``ContentProvider`` / ``BrowserProvider``), including the §11.4 ladder
extension methods (``accessibility_snapshot`` / ``interact`` / ``network_capture``).

The SDKs themselves (``exa``, ``perplexity-sdk``, ``tavily``, ``firecrawl``,
``playwright``) are imported *lazily* inside each constructor so this module
imports cleanly even when the ``investigate`` extra is not installed.  Real
clients are only constructed by :func:`dra.routing.providers.make_providers`
when ``ProviderMode.LIVE`` is requested **and** at least one provider API-key
env var is present (see ``_creds_reachable``); otherwise a clear error is
raised and tests skip cleanly — mirroring the dra#9 env-gated convention.

Mapping (best-fit real SDK per provider role; per-task name routing is the
registry's concern, not the provider set's):

* ``search``    → Exa (conceptual search) when EXA_API_KEY is set, else Perplexity
* ``content``   → Firecrawl (scrape/extract) when FIRECRAWL_API_KEY is set, else Tavily
* ``sitemap``   → Tavily (map/crawl) when TAVILY_API_KEY is set, else Firecrawl
* ``browser``   → Playwright (rendered DOM / AX tree / network / Har)

Playwright *browser binaries* are an operator-run concern
(``playwright install chromium``) — they are deliberately NOT a canonical
dependency (§17.3 non-goal); see decision D3.
"""

from __future__ import annotations

import os
from typing import Any

from dra.routing.providers import _LiveStubProvider

__all__ = [
    "ExaSearchClient",
    "PerplexitySearchClient",
    "TavilyClient",
    "FirecrawlClient",
    "PlaywrightBrowserClient",
    "make_live_providers",
]

# Env vars that gate each real client.
_ENV_EXA = "EXA_API_KEY"
_ENV_TAVILY = "TAVILY_API_KEY"
_ENV_FIRECRAWL = "FIRECRAWL_API_KEY"


_ERROR_MISSING_KEY = (
    "ProviderMode.LIVE requested but the API key for {client} is not set "
    "in env ({env}). Install the `investigate` extra and configure credentials."
)


# ---------------------------------------------------------------------------
# Search (Exa / Perplexity)
# ---------------------------------------------------------------------------


class ExaSearchClient:
    """Real Exa AI search/extraction client.

    The ``exa`` import resolves once the ``exa-py`` package is installed
    (``pip install exa-py``); the PyPI ``exa`` project is unrelated.
    """

    name = "exa"

    def __init__(self) -> None:
        if not os.environ.get(_ENV_EXA):
            raise RuntimeError(_ERROR_MISSING_KEY.format(client="Exa", env=_ENV_EXA))
        import exa  # type: ignore  # noqa: PLC0415 — lazy SDK import

        api_key = os.environ[_ENV_EXA]
        self._client = exa.Exa(api_key=api_key)  # type: ignore[attr-defined]

    async def search(
        self,
        query: str,
        *,
        k: int = 10,
        filters: dict[str, Any] | None = None,
        mode: str = "keyword",
        include_full_text: bool = False,
    ) -> list[dict]:
        results = await self._client.search(
            query,
            num_results=k,
            text=include_full_text,
            **({"filters": filters} if filters else {}),
        )
        out: list[dict] = []
        for r in getattr(results, "results", results):
            out.append(
                {
                    "title": getattr(r, "title", ""),
                    "url": getattr(r, "url", ""),
                    "snippet": getattr(r, "snippet", getattr(r, "text", "")),
                    "score": getattr(r, "score", 1.0),
                }
            )
        return out


class PerplexitySearchClient:
    """Perplexity Search client (lazy import).

    Perplexity AI does not publish an official PyPI SDK (the spec's
    ``perplexity-sdk`` does not exist on PyPI).  The closest community package
    is ``perplexityai``.  This client therefore raises a clear, explicit error
    at construction so a missing/mismatched SDK is never mistaken for an empty
    result set — the search role falls back to Exa (``exa-py``) when
    ``EXA_API_KEY`` is configured.
    """

    name = "perplexity"

    def __init__(self) -> None:
        raise RuntimeError(
            "Perplexity has no official PyPI SDK (the spec's 'perplexity-sdk' "
            "does not exist on PyPI). Install a community client, e.g. "
            "`pip install perplexityai`, and replace this stub, OR set "
            "EXA_API_KEY to use the Exa search client instead."
        )

    async def search(
        self,
        query: str,
        *,
        k: int = 10,
        filters: dict[str, Any] | None = None,
        mode: str = "keyword",
        include_full_text: bool = False,
    ) -> list[dict]:
        raise RuntimeError("PerplexitySearchClient is not wired (no SDK)")


# ---------------------------------------------------------------------------
# Content / Sitemap (Firecrawl / Tavily)
# ---------------------------------------------------------------------------


class FirecrawlClient:
    """Real Firecrawl scrape/extract/crawl/map client (lazy import)."""

    name = "firecrawl"

    def __init__(self) -> None:
        if not os.environ.get(_ENV_FIRECRAWL):
            raise RuntimeError(
                _ERROR_MISSING_KEY.format(client="Firecrawl", env=_ENV_FIRECRAWL)
            )
        import firecrawl  # type: ignore  # noqa: PLC0415

        self._client = firecrawl.FirecrawlClient(api_key=os.environ[_ENV_FIRECRAWL])

    async def fetch(self, url: str) -> dict:
        res = await self._client.scrape_url(url, formats=["html"])
        content = res.get("html", "") if isinstance(res, dict) else ""
        return {
            "url": url,
            "content": content,
            "mime_type": "text/html",
            "metadata": {"fetched_by": "firecrawl"},
            "dynamic_rendered": True,
        }

    async def extract(self, url: str) -> str:
        res = await self._client.scrape_url(url, formats=["text"])
        if isinstance(res, dict):
            return res.get("text", "")
        return ""

    async def sitemap(self, base_url: str, *, max_depth: int = 3) -> list[str]:
        res = await self._client.crawl_url(base_url, max_depth=max_depth)
        if isinstance(res, dict) and "urls" in res:
            return list(res["urls"])
        return []


class TavilyClient:
    """Real Tavily map/crawl/extract client (lazy import)."""

    name = "tavily"

    def __init__(self) -> None:
        if not os.environ.get(_ENV_TAVILY):
            raise RuntimeError(
                _ERROR_MISSING_KEY.format(client="Tavily", env=_ENV_TAVILY)
            )
        import tavily  # type: ignore  # noqa: PLC0415

        self._client = tavily.TavilyClient(api_key=os.environ[_ENV_TAVILY])

    async def fetch(self, url: str) -> dict:
        res = await self._client.crawl_url(url, max_pages=1)
        return {
            "url": url,
            "content": res.get("raw", "") if isinstance(res, dict) else "",
            "mime_type": "text/html",
            "metadata": {"fetched_by": "tavily"},
            "dynamic_rendered": False,
        }

    async def extract(self, url: str) -> str:
        res = await self._client.extract_url(url)
        if isinstance(res, dict):
            return res.get("text", "") or res.get("raw", "")
        return ""

    async def sitemap(self, base_url: str, *, max_depth: int = 3) -> list[str]:
        res = await self._client.crawl_url(base_url, max_depth=max_depth)
        if isinstance(res, dict) and "urls" in res:
            return list(res["urls"])
        return []


# ---------------------------------------------------------------------------
# Browser (Playwright) — covers the full §11.4 ladder extension
# ---------------------------------------------------------------------------


class PlaywrightBrowserClient:
    """Real Playwright browser provider (rendered DOM + §11.4 ladder).

    Browser binaries are an operator-run step (``playwright install chromium``);
    only the Python SDK is declared as a dependency.
    """

    name = "playwright"

    def __init__(self) -> None:
        # NOTE: no API-key env gate — Playwright uses a local browser binary.
        # The SDK import + browser launch are deferred to first use so that
        # constructing this client never crashes when only a *subset* of the
        # search/extraction SDKs are configured with keys (operator concern:
        # `playwright install chromium` must run before a browser step).
        self._playwright_ctx = None
        self._browser = None
        self._sessions: dict[str, Any] = {}

    def _ensure(self) -> Any:
        """Lazily start Playwright and launch the browser on first use."""
        if self._browser is not None:
            return self._browser
        from playwright.async_api import async_playwright  # noqa: PLC0415

        self._playwright_ctx = async_playwright().start()
        self._browser = self._playwright_ctx.chromium.launch()
        return self._browser

    async def open_session(self) -> str:
        import uuid  # noqa: PLC0415

        session_id = str(uuid.uuid4())
        browser = self._ensure()
        page = await browser.new_page()
        self._sessions[session_id] = page
        return session_id

    async def navigate(self, session_id: str, url: str) -> None:
        page = self._sessions[session_id]
        await page.goto(url)

    async def dom_snapshot(self, session_id: str) -> str:
        page = self._sessions[session_id]
        return await page.content()

    async def screenshot(self, session_id: str) -> bytes:
        page = self._sessions[session_id]
        return await page.screenshot()

    async def close_session(self, session_id: str) -> None:
        page = self._sessions.pop(session_id, None)
        if page is not None:
            await page.close()

    async def accessibility_snapshot(self, session_id: str) -> str:
        page = self._sessions[session_id]
        snapshot = await page.accessibility.snapshot()
        return _to_json(snapshot)

    async def interact(self, session_id: str, action: dict[str, Any]) -> dict[str, Any]:
        page = self._sessions[session_id]
        op = action.get("action")
        if op == "scroll":
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            return {"action": "scroll", "result": "ok"}
        if op == "click":
            await page.click(action.get("selector", ""))
            return {"action": "click", "selector": action.get("selector"), "result": "ok"}
        return {"action": op, "result": "noop"}

    async def network_capture(self, session_id: str, *, capture_har: bool = True) -> bytes:
        page = self._sessions[session_id]
        if capture_har:
            har = await page.context.tracing.start(
                screenshots=False, snapshots=True, sources=True
            )
            return har if isinstance(har, bytes) else _to_json_bytes(har)
        client = await page.context.new_cdp_session(page)
        events = await client.send("Network.getResponseBody", {})
        return _to_json_bytes(events)

    async def close_all(self) -> None:
        for page in list(self._sessions.values()):
            await page.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright_ctx is not None:
            self._playwright_ctx.stop()


def _to_json(obj: Any) -> str:
    import json  # noqa: PLC0415

    return json.dumps(obj)


def _to_json_bytes(obj: Any) -> bytes:
    return _to_json(obj).encode("utf-8")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_live_providers() -> dict[str, Any]:
    """Build a role-keyed set of real SDK clients (best-effort per role).

    The search role uses Exa (``exa-py``) when ``EXA_API_KEY`` is present;
    Perplexity has no official PyPI SDK and is not wired as a live client
    (see :class:`PerplexitySearchClient`), so absent an Exa key the search role
    falls back to the existing :class:`_LiveStubProvider`.  Live tests are
    credential-gated by the caller (``_creds_reachable`` in
    :mod:`dra.routing.providers``) and skip without keys.
    """
    search: Any = ExaSearchClient() if os.environ.get(_ENV_EXA) else _LiveStubProvider("search")
    content = (
        FirecrawlClient() if os.environ.get(_ENV_FIRECRAWL)
        else TavilyClient() if os.environ.get(_ENV_TAVILY)
        else _LiveStubProvider("content")
    )
    sitemap = (
        TavilyClient() if os.environ.get(_ENV_TAVILY)
        else FirecrawlClient() if os.environ.get(_ENV_FIRECRAWL)
        else _LiveStubProvider("sitemap")
    )
    browser = PlaywrightBrowserClient()
    return {
        "search": search,
        "content": content,
        "sitemap": sitemap,
        "browser": browser,
    }
