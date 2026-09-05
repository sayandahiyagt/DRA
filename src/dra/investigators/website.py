"""WebsiteInvestigator — the §11.4 Browser/DOM investigator (dra#26).

Given a task type and a set of target URLs, a :class:`WebsiteInvestigator`
routes through the task-routed provider matrix (:mod:`dra.routing.providers`),
runs the §11.4 escalation ladder (search snippet → extracted text → raw HTML →
rendered DOM → accessibility tree → interactive session → screenshots →
network/HAR), and enforces the §22 / ADR-015 access-policy gate on every
acquisition.

Wave 1b (dra#79) rewrites the evidence-emission contract using the Part 1
storage model, fixing the four behavioral flaws in evolution spec §36–39:

- **§36/§156** — ``source_identity`` is keyed by the exact canonical resource URL
  (the page URL), not the site origin; origin/publisher are recorded as metadata
  on ``source_representation`` so multiple pages on one site no longer collapse.
- **§37** — each search-engine snippet is bound to its own ``returned_url`` via a
  ``SourceCandidate`` record rather than attaching it to the investigated target
  URL path.
- **§38/§39** — discovery & acquisition observations (search_snippet, raw_html,
  screenshot, network_har) are separated from ``EvidenceUnit``→``Claim``:
  discovery rungs emit ``SourceCandidate`` / ``source_capture`` rows only, never
  a researched claim, unless the provider contract guarantees authoritative full
  content.
- **§D4** — HTML/screenshot/HAR raw bytes are persisted durably via the
  ``content_blob.storage_uri`` (BlobStore), not the non-durable ``stored_at``
  temp path.

Evidence is emitted exclusively through an :class:`~dra.investigators.InvestigatorContext`:
content-addressed ``source_capture`` rows, ``SourceCandidate`` discovery records,
normalized derived artifacts, ``evidence_unit``s with ``web`` locators, and
``claim``s — never equating DOM/network observation with private source truth.

Offline-first: by default the investigator uses the deterministic fakes from
``make_providers(ProviderMode.OFFLINE)`` so the capture → evidence_unit → claim
publish path is fully exercised without network or API keys.  Real SDKs are
env-gated behind ``ProviderMode.LIVE`` (see
:mod:`dra.investigators.providers_live`).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from dra.investigators import (
    InvestigatorContext,
    content_hash,
    normalize_locator,
    validate_locator,
)
from dra.investigators.access_policy import (
    DEFAULT_USER_AGENT,
    AccessPolicyGate,
    RobotsPolicy,
    SourceAccessBasis,
)
from dra.routing.providers import (
    ProviderMode,
    SearchProviderRegistry,
    make_providers,
)

__all__ = [
    "EVIDENCE_LABELS",
    "LADDER_STEPS",
    "InvestigationResult",
    "WebsiteInvestigator",
]


# ---------------------------------------------------------------------------
# §11.4 evidence labels (distinction without a source-of-truth equivalence)
# ---------------------------------------------------------------------------

#: The distinct evidence labels an investigator may emit (§11.4).  A claim
#: tagged with one of these labels is *observed* evidence, never private
#: source truth — DOM/network observation is treated as such and must be so
#: labeled.
EVIDENCE_LABELS: frozenset[str] = frozenset(
    {
        "direct DOM",
        "visible UI",
        "accessibility-tree",
        "network-observed",
        "inferred frontend architecture",
        "speculation",
    }
)


@dataclass(frozen=True)
class _LadderStep:
    """One rung of the §11.4 escalation ladder."""

    number: int
    name: str
    label: str
    requires_har_auth: bool  # step 8 (network/HAR) is authorized-only
    is_discovery: bool  # True = acquisition/discovery observation (§38/§39)


#: Ordered escalation ladder (§11.4 / spec §17.1).  Each rung has a distinct
#: evidence label so downstream consumers can weight reliability rather than
#: treating every observation as ground truth.
#:
#: ``is_discovery`` separates §38/§39 discovery & acquisition observations
#: (search_snippet, raw_html, screenshot, network_har — no EvidenceUnit→Claim)
#: from content rungs that feed reasoning (extracted_text, rendered_dom,
#: accessibility_tree, interactive_session — keep EvidenceUnit, asserts_private_truth=False).
LADDER_STEPS: tuple[_LadderStep, ...] = (
    _LadderStep(1, "search_snippet", "network-observed", False, is_discovery=True),
    _LadderStep(2, "extracted_text", "visible UI", False, is_discovery=False),
    _LadderStep(3, "raw_html", "direct DOM", False, is_discovery=True),
    _LadderStep(4, "rendered_dom", "direct DOM", False, is_discovery=False),
    _LadderStep(5, "accessibility_tree", "accessibility-tree", False, is_discovery=False),
    _LadderStep(6, "interactive_session", "inferred frontend architecture", False, is_discovery=False),
    _LadderStep(7, "screenshot", "visible UI", False, is_discovery=True),
    _LadderStep(8, "network_har", "network-observed", True, is_discovery=True),
)


class _SkipStep(Exception):
    """Raised by a ladder rung that produced no acquirable content."""


class _OriginThrottle:
    """Per-origin rate-limit throttle derived from RFC 9309 Crawl-delay.

    Enforces a minimum interval (seconds) between acquisitions against the same
    origin, so the investigator observes the ``rate_limit`` reported by the
    robots policy (§22 / ADR-015: "observe rate limits").  A non-positive or
    absent interval is a no-op (the default for offline tests).
    """

    def __init__(self) -> None:
        self._last: dict[str, float] = {}

    async def wait(self, origin: str, interval: float | None) -> None:
        if not interval or interval <= 0:
            return
        last = self._last.get(origin)
        now = time.monotonic()
        if last is not None:
            remaining = interval - (now - last)
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last[origin] = time.monotonic()



# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class InvestigationResult:
    """Summary of a single ``investigate`` run."""

    task_type: str
    query: str
    examined_urls: list[str] = field(default_factory=list)
    skipped_urls: list[dict[str, Any]] = field(default_factory=list)
    manifest_entries: list[dict[str, Any]] = field(default_factory=list)
    discovery_count: int = 0
    evidence_unit_count: int = 0
    claim_count: int = 0
    published_count: int | None = None


# ---------------------------------------------------------------------------
# WebsiteInvestigator
# ---------------------------------------------------------------------------

# Confidence the investigator attaches to each label class.  These weight the
# *reliability of the observed label* — they are NOT statements that the
# observation is private source truth, so claims never get reified as ground
# truth.
_LABEL_CONFIDENCE: dict[str, float] = {
    "direct DOM": 0.9,
    "visible UI": 0.8,
    "accessibility-tree": 0.85,
    "network-observed": 0.6,
    "inferred frontend architecture": 0.5,
    "speculation": 0.3,
}


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


class WebsiteInvestigator:
    """Task-routed provider consumer for §11.4 web investigation.

    Takes an already-open :class:`InvestigatorContext` (``ctx``) and a provider
    set; it never opens its own bundle.  The caller owns the bundle lifecycle so
    the staged→canonical commit (ADR-013) and rollback semantics stay unified
    with every other investigator.

    Construction is intentionally explicit (no dataclass-generated ``__init__``)
    so the offline-vs-LIVE provider wiring and the access-policy gate stay
    co-located and easy to audit.
    """

    def __init__(
        self,
        *,
        registry: SearchProviderRegistry | None = None,
        providers: dict[str, Any] | None = None,
        provider_mode: ProviderMode = ProviderMode.OFFLINE,
        user_agent: str = DEFAULT_USER_AGENT,
        robots_fetcher: Callable[[str], str | None] | None = None,
        license_resolver: Callable[[str], str | None] | None = None,
        default_access_basis: SourceAccessBasis = SourceAccessBasis.PUBLIC,
        har_authorized: bool = False,
        concurrency: int = 4,
        fixture_results: dict[str, list[dict]] | None = None,
        page_content: dict[str, str] | None = None,
    ) -> None:
        self.registry = registry or SearchProviderRegistry()
        if providers is None:
            providers = make_providers(
                provider_mode,
                fixture_results=fixture_results,
                page_content=page_content,
            )
        self.providers = providers
        self.user_agent = user_agent
        self.gate = AccessPolicyGate(
            robots=RobotsPolicy(fetcher=robots_fetcher, user_agent=user_agent),
            license_resolver=license_resolver,
            default_access_basis=default_access_basis,
        )
        self.har_authorized = har_authorized
        self.concurrency = max(1, concurrency)
        self._throttle = _OriginThrottle()

    # -- public API ---------------------------------------------------------

    async def investigate(
        self,
        ctx: InvestigatorContext,
        *,
        task_type: str,
        query: str,
        target_urls: list[str],
        max_step: int = 8,
        needs_more: Callable[[str | None], bool] | None = None,
        har_authorized: bool | None = None,
        use_sitemap: bool = False,
    ) -> InvestigationResult:
        """Run the §11.4 ladder for each target URL within ``ctx``.

        ``max_step`` bounds escalation (§17.1 step 10: no blind HAR capture —
        step 8 only runs when ``har_authorized`` is true).  ``needs_more``,
        if given, short-circuits the ladder when a step's content is judged
        sufficient; the default keeps escalating up to ``max_step``.
        """
        har = self.har_authorized if har_authorized is None else har_authorized
        result = InvestigationResult(
            task_type=task_type, query=query, examined_urls=list(target_urls)
        )
        sem = asyncio.Semaphore(self.concurrency)

        if use_sitemap:
            await self._sitemap_preprocess(target_urls, result)

        async def _one(url: str) -> None:
            async with sem:
                await self._investigate_url(
                    ctx, url, task_type, query, max_step, needs_more, har, result
                )

        await asyncio.gather(*(_one(u) for u in target_urls))
        result.published_count = ctx.published_count
        return result

    async def _sitemap_preprocess(
        self, target_urls: list[str], result: InvestigationResult
    ) -> None:
        """Prefer sitemap/map endpoints before blind crawling (§11.4).

        Best-effort: expands the target set from sitemaps per origin where the
        provider supports it (offline fakes return no sitemap capability, so
        this is a no-op for the deterministic proof path).
        """
        seen_origins: set[str] = set()
        for url in list(target_urls):
            origin = _origin(url)
            if origin in seen_origins:
                continue
            seen_origins.add(origin)
            discovered = await self.discover_via_sitemap(origin)
            for new_url in discovered:
                if new_url not in target_urls:
                    target_urls.append(new_url)
                    result.examined_urls.append(new_url)

    # -- per-URL ladder -----------------------------------------------------

    async def _investigate_url(
        self,
        ctx: InvestigatorContext,
        url: str,
        task_type: str,
        query: str,
        max_step: int,
        needs_more: Callable[[str | None], bool] | None,
        har_authorized: bool,
        result: InvestigationResult,
    ) -> None:
        origin = _origin(url)
        t0 = time.monotonic()

        # §22 / ADR-015 access-policy gate FIRST — never fetch when RFC 9309
        # disallows crawl.  crawl_allowed is the *policy result*, not
        # authorization to publish/distribute.
        policy, source_id = await self._evaluate_policy(ctx, url, origin, task_type)
        if not policy["crawl_allowed"]:
            await self._record_crawl_manifest_db(ctx, url, origin, "skipped",
                                                  "policy_gate", policy["reason"])
            self._log_manifest(
                result, url=url, origin=origin, result_status="skipped",
                step="policy_gate", reason=policy["reason"],
                latency_ms=round((time.monotonic() - t0) * 1000, 3),
            )
            return

        # Observe the RFC 9309 Crawl-delay rate limit (best-effort, per-origin).
        await self._throttle.wait(origin, policy["rate_limit"])

        await self._record_crawl_manifest_db(
            ctx, url, origin, "attempted", "policy_gate", None
        )
        self._log_manifest(
            result, url=url, origin=origin, result_status="attempted",
            step="policy_gate", reason=None,
            latency_ms=round((time.monotonic() - t0) * 1000, 3),
        )

        last_content: str | None = None
        for step in LADDER_STEPS:
            if step.number > max_step:
                break
            if step.requires_har_auth and not har_authorized:
                await self._record_crawl_manifest_db(
                    ctx, url, origin, "skipped", step.name,
                    "HAR capture requires explicit authorization (§22.2)",
                    metadata={"ladder_step": step.name, "label": step.label},
                )
                self._log_manifest(
                    result, url=url, origin=origin, result_status="skipped",
                    step=step.name,
                    reason="HAR capture requires explicit authorization (§22.2)",
                    latency_ms=round((time.monotonic() - t0) * 1000, 3),
                    metadata={"ladder_step": step.name},
                )
                break
            try:
                content = await self._run_step(
                    ctx, source_id, url, origin, policy, query, task_type, step
                )
            except _SkipStep:
                self._log_manifest(
                    result, url=url, origin=origin, result_status="skipped",
                    step=step.name, reason="step produced no acquirable content",
                    latency_ms=round((time.monotonic() - t0) * 1000, 3),
                )
                break
            await self._record_crawl_manifest_db(
                ctx, url, origin, "crawled", step.name, None,
                metadata={
                    "evidence_label": step.label,
                    "ladder_step": step.name,
                    "content_hash": (
                        content_hash(content) if content is not None else None
                    ),
                },
            )
            self._log_manifest(
                result, url=url, origin=origin, result_status="crawled",
                step=step.name, reason=None,
                latency_ms=round((time.monotonic() - t0) * 1000, 3),
                content_hash=content_hash(content) if content is not None else None,
            )
            last_content = content if isinstance(content, str) else None
            if step.is_discovery:
                result.discovery_count += 1
            else:
                result.evidence_unit_count += 1
                result.claim_count += 1
            if needs_more is not None and not needs_more(last_content):
                break

    # -- policy -------------------------------------------------------------

    async def _evaluate_policy(
        self, ctx: InvestigatorContext, url: str, origin: str, task_type: str
    ) -> tuple[dict[str, Any], UUID]:
        policy = await self._gate_evaluate(url, task_type=task_type)
        source_id = await ctx.stage_source_identity(
            kind="web",
            locator=url,
            version=None,
            license_spdx=policy["license_spdx"],
            access_basis=policy["access_basis"],
            crawl_allowed=policy["crawl_allowed"],
            auth_scope=policy["auth_scope"],
            redist_allowed=policy["redist_allowed"],
            metadata={
                "user_agent": self.user_agent,
                "origin": origin,
                "robots_directive": policy["robots_directive"],
                "rate_limit": policy["rate_limit"],
                "sitemaps": policy["sitemaps"],
            },
        )
        return policy, source_id

    async def _gate_evaluate(
        self, url: str, *, task_type: str
    ) -> dict[str, Any]:
        res = self.gate.evaluate(url, task_type=task_type, kind="web")
        return {
            "access_basis": res.access_basis,
            "crawl_allowed": res.crawl_allowed,
            "robots_directive": res.robots_directive.value,
            "rate_limit": res.rate_limit,
            "auth_scope": res.auth_scope,
            "license_spdx": res.license_spdx,
            "redist_allowed": res.redist_allowed,
            "sitemaps": list(res.sitemaps),
            "reason": res.reason,
        }

    # -- crawl manifest -----------------------------------------------------

    async def _record_crawl_manifest_db(
        self,
        ctx: InvestigatorContext,
        url: str,
        origin: str,
        result_status: str,
        step: str | None,
        reason: str | None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await ctx.stage_crawl_manifest_entry(
            url=url, origin=origin, result=result_status, step=step,
            reason=reason, metadata=metadata,
        )

    def _log_manifest(
        self, result: InvestigationResult, *, url: str, origin: str,
        result_status: str, step: str | None, reason: str | None,
        latency_ms: float | None, content_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "url": url,
            "origin": origin,
            "result": result_status,
            "step": step,
            "reason": reason,
            "latency_ms": latency_ms,
            "content_hash": content_hash,
            "metadata": metadata or {},
        }
        result.manifest_entries.append(entry)

    # -- ladder steps -------------------------------------------------------

    async def _run_step(
        self,
        ctx: InvestigatorContext,
        source_id: UUID,
        url: str,
        origin: str,
        policy: dict[str, Any],
        query: str,
        task_type: str,
        step: _LadderStep,
    ) -> Any:
        """Execute one ladder rung; return the raw content (or raise _SkipStep)."""
        label = step.label
        if step.name == "search_snippet":
            result = await self._snippet(query)
            if not result:
                raise _SkipStep()
            returned_url = result.get("url", "")
            snippet = result.get("snippet", "") or ""
            await ctx.stage_source_candidate(
                query=query,
                purpose=task_type,
                provider=(
                    self.providers["search"].name
                    if "search" in self.providers
                    else None
                ),
                title=result.get("title"),
                returned_url=returned_url,
                snippet=snippet if snippet else None,
                rank=result.get("rank", 0),
                provider_score=result.get("score"),
                origin=_origin(returned_url) if returned_url else None,
                metadata={
                    "evidence_label": label,
                    "ladder_step": step.name,
                    "task_type": task_type,
                },
            )
            return snippet
        if step.name == "extracted_text":
            text = await self._extract(url)
            await self._record_evidence(
                ctx, source_id, url, origin, "text", text, label=label,
                step=step.name,
                claim_text=f"[{label}] Visible text extracted: {text[:200]}",
                mime="text/plain", dom_locator="document", derived_text=text,
            )
            return text
        if step.name == "raw_html":
            fetched = await self._fetch(url)
            html = fetched.get("content", "")
            mime = fetched.get("mime_type") or "text/html"
            await self._record_acquisition_capture(
                ctx, source_id, url, origin, "html", html, label=label,
                step=step.name, mime=mime, dom_locator="document",
            )
            return html
        if step.name == "rendered_dom":
            dom = await self._dom_snapshot(url)
            if not dom:
                raise _SkipStep()
            await self._record_evidence(
                ctx, source_id, url, origin, "html", dom, label=label,
                step=step.name,
                claim_text=f"[{label}] Rendered DOM captured from {url}",
                mime="text/html", dom_locator="rendered-document",
            )
            return dom
        if step.name == "accessibility_tree":
            xml = await self._accessibility(url)
            if not xml:
                raise _SkipStep()
            await self._record_evidence(
                ctx, source_id, url, origin, "xml", xml, label=label,
                step=step.name,
                claim_text=f"[{label}] Accessibility tree observed for {url}",
                mime="application/xml", dom_locator="ax-tree",
            )
            return xml
        if step.name == "interactive_session":
            observed = await self._interact(url)
            if not observed:
                raise _SkipStep()
            await self._record_evidence(
                ctx, source_id, url, origin, "text", observed,
                label="inferred frontend architecture", step=step.name,
                claim_text=f"[speculation] Frontend architecture inferred from {url}",
                mime="text/plain", dom_locator="interactive-state",
                confidence=_LABEL_CONFIDENCE["speculation"],
            )
            return observed
        if step.name == "screenshot":
            img = await self._screenshot(url)
            if not img:
                raise _SkipStep()
            await self._record_acquisition_capture(
                ctx, source_id, url, origin, "image", img, label=label,
                step=step.name, mime="image/png", dom_locator="viewport",
            )
            return img
        if step.name == "network_har":
            har = await self._network_capture(url)
            if not har:
                raise _SkipStep()
            await self._record_acquisition_capture(
                ctx, source_id, url, origin, "xml", har, label=label,
                step=step.name, mime="application/json", dom_locator="network-log",
            )
            return har
        raise _SkipStep()

    # -- provider delegations ----------------------------------------------

    async def _snippet(self, query: str) -> dict[str, Any] | None:
        """Return the full first search result dict (url + snippet + score).

        Returns the complete result so the caller can bind the snippet to its
        own ``returned_url`` as a SourceCandidate (§37) rather than attaching it
        to the investigated target URL path.
        """
        search = self.providers.get("search")
        if search is None:
            return None
        results = await search.search(query, k=5)
        if not results:
            return None
        first = results[0] if isinstance(results[0], dict) else {}
        return first or None

    async def _extract(self, url: str) -> str:
        content = self.providers.get("content")
        if content is None:
            return ""
        return await content.extract(url)

    async def _fetch(self, url: str) -> dict:
        content = self.providers.get("content")
        if content is None:
            return {"content": "", "mime_type": "text/html", "metadata": {}}
        return await content.fetch(url)

    async def _with_browser(self, url: str, op: Callable) -> Any:
        browser = self.providers.get("browser")
        if browser is None:
            return None
        session = await browser.open_session()
        try:
            await browser.navigate(session, url)
            return await op(browser, session)
        finally:
            await browser.close_session(session)

    async def _dom_snapshot(self, url: str) -> str:
        res = await self._with_browser(url, lambda b, s: b.dom_snapshot(s))
        return res or ""

    async def _accessibility(self, url: str) -> str:
        res = await self._with_browser(
            url, lambda b, s: b.accessibility_snapshot(s)
        )
        return res or ""

    async def _interact(self, url: str) -> str:
        async def _op(b, s):
            res = await b.interact(s, {"action": "scroll"})
            return f"{url} interactive state: {res}"
        return await self._with_browser(url, _op) or ""

    async def _screenshot(self, url: str) -> bytes:
        res = await self._with_browser(url, lambda b, s: b.screenshot(s))
        return res if isinstance(res, bytes) else b""

    async def _network_capture(self, url: str) -> bytes:
        async def _op(b, s):
            return await b.network_capture(s, capture_har=True)
        res = await self._with_browser(url, _op)
        return res if isinstance(res, bytes) else b""

    # -- capture staging ----------------------------------------------------

    async def _record_acquisition_capture(
        self,
        ctx: InvestigatorContext,
        source_id: UUID,
        url: str,
        origin: str,
        kind: str,
        content: str | bytes,
        *,
        label: str,
        step: str,
        mime: str | None = None,
        dom_locator: str = "document",
    ) -> str:
        """Stage raw acquisition bytes only (§39: acquisition is an observation).

        Used by discovery/acquisition-observation rungs (``raw_html``,
        ``screenshot``, ``network_har``): stages a durable ``source_capture``
        (content_blob + source_representation + source_capture) with
        ``origin``/``publisher`` on the representation (§156), but does **not**
        create a ``derived_artifact`` / ``evidence_unit`` / ``claim``.  These are
        acquisition-provenance events, not researched conclusions (§39).
        """
        if content is None:
            content = b"" if isinstance(content, bytes) else ""
        raw_h = content_hash(content)
        await ctx.stage_source_capture(
            source_id,
            raw_h,
            kind=kind,
            mime_type=mime,
            data=content.encode("utf-8") if isinstance(content, str) else content,
            final_url=url,
            origin=origin,
            publisher=None,
            metadata={
                "evidence_label": label,
                "ladder_step": step,
                "canonical_url": url,
                "asserts_private_truth": False,
            },
        )
        return raw_h

    async def _record_evidence(
        self,
        ctx: InvestigatorContext,
        source_id: UUID,
        url: str,
        origin: str,
        kind: str,
        content: str | bytes,
        *,
        label: str,
        step: str,
        claim_text: str,
        mime: str | None = None,
        dom_locator: str = "document",
        derived_text: str | None = None,
        confidence: float | None = None,
    ) -> tuple[str, UUID, UUID, UUID]:
        """Stage raw → derived → evidence_unit → claim for one observation (§38/§39).

        Used by content rungs that feed reasoning (``extracted_text``,
        ``rendered_dom``, ``accessibility_tree``, ``interactive_session``): these
        keep ``EvidenceUnit``→``Claim`` but the claim is tagged with its evidence
        label and records ``asserts_private_truth=False`` so no DOM/network
        observation is reified as source-of-truth.
        """
        if content is None:
            content = ""
        raw_h = content_hash(content)
        await ctx.stage_source_capture(
            source_id,
            raw_h,
            kind=kind,
            mime_type=mime,
            data=content.encode("utf-8") if isinstance(content, str) else content,
            final_url=url,
            origin=origin,
            publisher=None,
            metadata={
                "evidence_label": label,
                "ladder_step": step,
                "canonical_url": url,
            },
        )

        if isinstance(content, str):
            text = derived_text if derived_text is not None else content
        else:
            text = (
                derived_text
                if derived_text is not None
                else f"<{kind} capture for {url}>"
            )

        # Derived content-hash is scoped to the producing ladder step as well as
        # the text: two steps can yield byte-identical extracted text yet remain
        # distinct artifacts (different provenance).  This also keeps distinct
        # `prov_entity` rows from colliding on derived_artifact's
        # UNIQUE(content_hash, kind, version) upsert inside one publish batch
        # (otherwise the second staging's table row is skipped while its
        # prov_entity remains → PublishError on validation).
        der_h = content_hash(f"{step}\n{text}")
        da_id = await ctx.stage_derived_artifact(
            raw_h, der_h, "normalized", version=1,
            metadata={"text": text, "evidence_label": label, "extracted_text": text},
        )

        locator = normalize_locator(
            "web",
            {
                "canonical_url": url,
                "captured_artifact": raw_h,
                "dom_locator": dom_locator,
                "text_locator": text[:300] if isinstance(text, str) else str(text),
            },
        )
        validate_locator("web", locator)
        ev_id = await ctx.stage_evidence_unit(
            da_id, locator, content_hash=der_h,
            metadata={"evidence_label": label, "ladder_step": step},
        )

        conf = confidence if confidence is not None else _LABEL_CONFIDENCE.get(label, 0.5)
        claim_id = await ctx.stage_claim(
            claim_text,
            evidence_unit_id=ev_id,
            confidence=conf,
            metadata={
                "evidence_label": label,
                "source_label": label,
                "asserts_private_truth": False,
                "ladder_step": step,
            },
        )
        return raw_h, da_id, ev_id, claim_id
