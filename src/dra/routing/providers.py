"""Provider contracts + task-routed search-provider matrix (§18, §17.2).

Defines the ``SearchProvider`` / ``SiteMapProvider`` / ``ContentProvider`` /
``BrowserProvider`` interfaces from §18 as :class:`typing.Protocol` classes, a
``TaskType`` enumeration of the roles the orchestrator (part 5) and investigators
(part 2) dispatch, and a ``SearchProviderRegistry`` that maps each task type to a
preferred provider ordered-list — the task-routed matrix over Exa / Perplexity /
Tavily / Firecrawl + rendered-browser fallback (§17.2).

Offline-first (D1): by default ``make_providers(ProviderMode.OFFLINE)`` returns
deterministic :class:`FakeSearchProvider` / :class:`FakeContentProvider` /
:class:`FakeBrowserProvider` implementations that require no network and no API
keys. Real SDKs are wired behind ``ProviderMode.LIVE`` and are credential-gated
(stubbed — real clients are instantiated only when the matching env vars are
present; see ``_creds_reachable``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Task taxonomy
# ---------------------------------------------------------------------------


class TaskType(str, Enum):
    """Research tasks that drive task-routed provider selection (§17.2).

    Mirrors the §23.2 candidate-task lists so the same taxonomy drives both
    model-pool selection and search-provider selection.
    """

    REPO_INVESTIGATION = "repo_investigation"
    PAPER_RECONCILIATION = "paper_reconciliation"
    DOM_REASONING = "dom_reasoning"
    FACT_EXTRACTION = "fact_extraction"
    BROWSING = "browsing"


# ---------------------------------------------------------------------------
# Provider mode
# ---------------------------------------------------------------------------


class ProviderMode(str, Enum):
    """Backend resolution mode for the proof harness (D1).

    ``OFFLINE`` (default) — deterministic fakes, no network, no keys.
    ``LIVE`` — real provider SDKs, credential-gated (skipped without creds).
    """

    OFFLINE = "offline"
    LIVE = "live"


# ---------------------------------------------------------------------------
# Provider contracts (§18 capability lists)
# ---------------------------------------------------------------------------


@runtime_checkable
class SearchProvider(Protocol):
    """Ranked search over a search provider's index (§18).

    Capabilities: ranked search; query filters; date/domain/source filters;
    multi-query; semantic/keyword modes; source metadata; optional
    highlights/full text.
    """

    name: str

    async def search(
        self,
        query: str,
        *,
        k: int = 10,
        filters: dict[str, Any] | None = None,
        mode: str = "keyword",
        include_full_text: bool = False,
    ) -> list[dict]:
        """Return ``k`` ranked result dicts (keys: title, url, snippet, score)."""
        ...


@runtime_checkable
class SiteMapProvider(Protocol):
    """Discover site/page topology without full extraction (§18)."""

    name: str

    async def sitemap(self, base_url: str, *, max_depth: int = 3) -> list[str]:
        """Return candidate URLs within scope/depth rules."""
        ...


@runtime_checkable
class ContentProvider(Protocol):
    """Fetch/extract content from a URL with full-text/HTML preservation (§18)."""

    name: str

    async def fetch(self, url: str) -> dict:
        """Return {url, content, mime_type, metadata, dynamic_rendered}."""
        ...

    async def extract(self, url: str) -> str:
        """Return extracted plain text from a URL."""
        ...


@runtime_checkable
class BrowserProvider(Protocol):
    """Canonical rendered-interaction interface (§17.2 / §18).

    Capabilities: session create/close; navigate; DOM snapshot; accessibility
    snapshot; screenshot; controlled interaction; network capture; artifact export.
    """

    name: str

    async def open_session(self) -> str:
        """Create a browser session; return a session handle."""
        ...

    async def navigate(self, session_id: str, url: str) -> None:
        """Navigate the session to a URL."""
        ...

    async def dom_snapshot(self, session_id: str) -> str:
        """Return a DOM snapshot as HTML text."""
        ...

    async def screenshot(self, session_id: str) -> bytes:
        """Return a PNG screenshot of the page."""
        ...

    async def close_session(self, session_id: str) -> None:
        """Close the browser session."""
        ...


# ---------------------------------------------------------------------------
# Fake (offline) providers — deterministic, fixture-aligned (D1, D5)
# ---------------------------------------------------------------------------


@dataclass
class _FakeResult:
    """Minimal deterministic search result."""

    title: str
    url: str
    snippet: str
    score: float


class FakeSearchProvider:
    """Deterministic offline SearchProvider returning scripted results.

    The results are aligned to the fixture set (see :mod:`dra.routing.fixtures`):
    each fixture carries ``source_refs`` — fake URLs that the fake provider
    returns in a stable, deterministic order. No real network is used.
    """

    name = "fake_search"

    def __init__(self, fixture_results: dict[str, list[dict]] | None = None) -> None:
        self._results = fixture_results or {}

    async def search(
        self,
        query: str,
        *,
        k: int = 10,
        filters: dict[str, Any] | None = None,
        mode: str = "keyword",
        include_full_text: bool = False,
    ) -> list[dict]:
        # Deterministic: return pre-aligned results for this query (matched by
        # fixture_id prefix), padded with generic placeholders if short.
        key = query
        results = list(self._results.get(key, []))
        if not results:
            results = [
                {
                    "title": f"Result {i} for '{query}'",
                    "url": f"https://example.org/{i}",
                    "snippet": f"Synthetic snippet {i}.",
                    "score": 1.0 - i * 0.01,
                }
                for i in range(k)
            ]
        # Score-normalise + truncate
        for i, r in enumerate(results):
            r.setdefault("score", 1.0 - i * 0.001)
        return results[:k]


class FakeContentProvider:
    """Deterministic offline ContentProvider returning scripted content."""

    name = "fake_content"

    def __init__(self, page_content: dict[str, str] | None = None) -> None:
        self._pages = page_content or {}

    async def fetch(self, url: str) -> dict:
        content = self._pages.get(url, f"<p>Content for {url}</p>")
        return {
            "url": url,
            "content": content,
            "mime_type": "text/html",
            "metadata": {"fetched_by": "fake_content"},
            "dynamic_rendered": False,
        }

    async def extract(self, url: str) -> str:
        return self._pages.get(url, f"Text content for {url}")


class FakeBrowserProvider:
    """Deterministic offline BrowserProvider (Playwright-style contract stub)."""

    name = "fake_browser"

    async def open_session(self) -> str:
        return "fake-session-0"

    async def navigate(self, session_id: str, url: str) -> None:
        pass

    async def dom_snapshot(self, session_id: str) -> str:
        return "<html><body>fake-dom</body></html>"

    async def screenshot(self, session_id: str) -> bytes:
        return b"fake-png-bytes"

    async def close_session(self, session_id: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Registry: task → ordered provider candidates (the task-routed matrix, §17.2)
# ---------------------------------------------------------------------------


@dataclass
class ProviderCandidate:
    """A named provider entry in the task-routed matrix."""

    name: str
    provider_type: str  # "search" | "sitemap" | "content" | "browser"
    priority: int


_TASK_ROUTED_MATRIX: dict[TaskType, list[ProviderCandidate]] = {
    TaskType.REPO_INVESTIGATION: [
        ProviderCandidate("github_search", "search", 1),
        ProviderCandidate("exa", "search", 2),
        ProviderCandidate("tavily", "content", 3),
        ProviderCandidate("rendered_browser", "browser", 4),
    ],
    TaskType.PAPER_RECONCILIATION: [
        ProviderCandidate("exa", "search", 1),
        ProviderCandidate("perplexity", "search", 2),
        ProviderCandidate("firecrawl", "content", 3),
        ProviderCandidate("rendered_browser", "browser", 4),
    ],
    TaskType.DOM_REASONING: [
        ProviderCandidate("perplexity", "search", 1),
        ProviderCandidate("firecrawl", "content", 2),
        ProviderCandidate("tavily", "sitemap", 3),
        ProviderCandidate("rendered_browser", "browser", 4),
    ],
    TaskType.FACT_EXTRACTION: [
        ProviderCandidate("tavily", "search", 1),
        ProviderCandidate("firecrawl", "content", 2),
        ProviderCandidate("exa", "search", 3),
        ProviderCandidate("rendered_browser", "browser", 4),
    ],
    TaskType.BROWSING: [
        ProviderCandidate("firecrawl", "content", 1),
        ProviderCandidate("tavily", "sitemap", 2),
        ProviderCandidate("rendered_browser", "browser", 3),
    ],
}


class SearchProviderRegistry:
    """Maps ``TaskType`` → ordered provider candidates (§17.2 task routing).

    ``select_providers(task_type)`` returns the ordered list of candidate names
    for a task. Rendered-browser is always the final fall-through when content
    APIs are insufficient (§17.2).
    """

    def __init__(self) -> None:
        self._matrix: dict[TaskType, list[ProviderCandidate]] = {
            k: list(v) for k, v in _TASK_ROUTED_MATRIX.items()
        }

    def select_providers(self, task_type: TaskType) -> list[ProviderCandidate]:
        """Return the ordered provider candidates for ``task_type``.

        Falls back to the full matrix (search → content → browser) for unknown
        task types.
        """
        return list(self._matrix.get(task_type, []))

    def provider_names(self, task_type: TaskType) -> list[str]:
        """Return just the names of the task-routed providers."""
        return [c.name for c in self.select_providers(task_type)]

    def has_rendered_browser_fallback(self, task_type: TaskType) -> bool:
        """True if rendered-browser is the final fall-through for ``task_type``."""
        cands = self.select_providers(task_type)
        return bool(cands) and cands[-1].provider_type == "browser"


# ---------------------------------------------------------------------------
# Credential / reachability gates (D1)
# ---------------------------------------------------------------------------


def _creds_reachable() -> bool:
    """True if a live-provider credential env var is present.

    Checked by tests that exercise real OpenAI/Anthropic/Google/search SDKs —
    skipped when absent (mirrors the ``tests/_db.py`` skipif convention for DB).
    """
    live_keys = [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "EXA_API_KEY",
        "PERPLEXITY_API_KEY",
        "TAVILY_API_KEY",
        "FIRECRAWL_API_KEY",
    ]
    return any(os.environ.get(k) for k in live_keys)


# ---------------------------------------------------------------------------
# Factory: offline fakes or live SDK clients (D1)
# ---------------------------------------------------------------------------


def make_providers(
    mode: ProviderMode = ProviderMode.OFFLINE,
    *,
    fixture_results: dict[str, list[dict]] | None = None,
    page_content: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a provider set for the given mode.

    ``OFFLINE`` (default) returns deterministic fakes aligned to the fixture
    set — no network, no keys. ``LIVE`` instantiates real SDK clients behind
    env-gated credentials; the stubs here raise if credentials are missing so
    failures are explicit rather than silently producing empty results.
    """
    if mode is ProviderMode.OFFLINE:
        return {
            "search": FakeSearchProvider(fixture_results),
            "sitemap": FakeSearchProvider(fixture_results),
            "content": FakeContentProvider(page_content),
            "browser": FakeBrowserProvider(),
        }
    # LIVE: real SDKs are stubs — wired only when credentials are present.
    if not _creds_reachable():
        raise RuntimeError(
            "ProviderMode.LIVE requested but no provider API key env vars "
            "are set (OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY, "
            "EXA_API_KEY, PERPLEXITY_API_KEY, TAVILY_API_KEY, FIRECRAWL_API_KEY)."
        )
    providers: dict[str, Any] = {}
    # Real SDK wiring is intentionally deferred — part 2/part 5 wire actual
    # clients through these contracts. The offline proof is the deliverable.
    for key in ("search", "sitemap", "content", "browser"):
        providers[key] = _LiveStubProvider(key, mode=mode)
    return providers


@dataclass
class _LiveStubProvider:
    """Placeholder for real SDK clients (env-gated, not wired in this proof)."""

    role: str
    mode: ProviderMode = field(default=ProviderMode.LIVE)

    async def search(self, query: str, **kw: Any) -> list[dict]:
        raise NotImplementedError("LIVE provider SDK wiring is out of scope for dra#9")

    async def fetch(self, url: str) -> dict:
        raise NotImplementedError("LIVE provider SDK wiring is out of scope for dra#9")

    async def extract(self, url: str) -> str:
        raise NotImplementedError("LIVE provider SDK wiring is out of scope for dra#9")

    async def sitemap(self, base_url: str, **kw: Any) -> list[str]:
        raise NotImplementedError("LIVE provider SDK wiring is out of scope for dra#9")

    async def open_session(self) -> str:
        raise NotImplementedError

    async def navigate(self, session_id: str, url: str) -> None:
        raise NotImplementedError

    async def dom_snapshot(self, session_id: str) -> str:
        raise NotImplementedError

    async def screenshot(self, session_id: str) -> bytes:
        raise NotImplementedError

    async def close_session(self, session_id: str) -> None:
        raise NotImplementedError
