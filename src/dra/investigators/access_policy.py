"""Access-policy gate for web acquisition (§22 / ADR-015).

Pure, dependency-light module (no DB, no third-party HTTP client beyond the
standard library).  It implements two things the §11.4 Browser/DOM Investigator
needs before fetching any page:

1. A minimal :class:`RobotsPolicy` that fetches and parses ``robots.txt``
   per RFC 9309 (User-agent / Allow / Disallow / Crawl-delay / Sitemap).  It
   returns an explicit *directive* (``ALLOWED`` / ``DISALLOWED`` /
   ``NO_ROBOTS_TXT``) rather than a bare boolean so callers can never mistake a
   crawl-policy decision for authorization — §22.2 is explicit that robots
   permission is necessary but **never sufficient** for acquisition.

2. An :class:`AccessPolicyGate` that combines the robots result with
   best-effort license detection and a declared access basis, producing an
   :class:`AccessPolicyResult` whose fields map exactly onto the
   ``source_identity`` staging columns (§22.1): ``access_basis``,
   ``crawl_allowed``, ``auth_scope``, ``license_spdx``, ``redist_allowed``.

The robots ``fetcher`` and ``license_resolver`` are injectable so the gate is
fully deterministic and unit-testable without network (see
``tests/test_website_investigator.py``).
"""

from __future__ import annotations

import enum
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse, urljoin

__all__ = [
    "SourceAccessBasis",
    "RobotsDirective",
    "RobotsPolicyResult",
    "AccessPolicyResult",
    "RobotsPolicy",
    "AccessPolicyGate",
    "DEFAULT_USER_AGENT",
]

DEFAULT_USER_AGENT = "dra-investigator/0.1"


# ---------------------------------------------------------------------------
# Access basis (§22.1) — how the source is reached.
# ---------------------------------------------------------------------------


class SourceAccessBasis(str, enum.Enum):
    """How a web source is reached / what scope its content has (§22.1).

    ``public``        — served to everyone, no auth.
    ``authenticated`` — requires an authenticated session for this account.
    ``user_provided`` — supplied by a human actor directly.
    ``org_internal``  — reachable only within the operator's org/network.
    ``restricted``    — known license / ToU restriction on reuse.
    """

    PUBLIC = "public"
    AUTHENTICATED = "authenticated"
    USER_PROVIDED = "user_provided"
    ORG_INTERNAL = "org_internal"
    RESTRICTED = "restricted"


# ---------------------------------------------------------------------------
# RFC 9309 robots.txt parsing (minimal, dependency-free)
# ---------------------------------------------------------------------------


class RobotsDirective(str, enum.Enum):
    """Outcome of an RFC 9309 robot-policy evaluation for one URL."""

    ALLOWED = "allowed"
    DISALLOWED = "disallowed"
    NO_ROBOTS_TXT = "no_robots_txt"


@dataclass
class _RobotRule:
    path: str
    allow: bool  # True = Allow, False = Disallow


@dataclass
class _RobotGroup:
    user_agents: list[str]
    rules: list[_RobotRule]
    crawl_delay: float | None


@dataclass
class RobotsPolicyResult:
    """The robots.txt evaluation result for a single URL (RFC 9309)."""

    directive: RobotsDirective
    crawl_allowed: bool  # True iff directive == ALLOWED
    rate_limit: float | None  # seconds, from Crawl-delay
    sitemaps: list[str] = field(default_factory=list)


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _robots_url(url: str) -> str:
    o = urlparse(url)
    return f"{o.scheme}://{o.netloc}/robots.txt"


def _default_fetcher(robots_txt_url: str) -> str | None:
    """Fetch ``robots.txt`` over HTTP with a short timeout.

    Returns the text, or ``None`` when the host has no robots.txt (HTTP 404 /
    connection error).  This is the only I/O boundary in the module; tests
    inject a fake ``fetcher`` to stay network-free.
    """
    try:
        req = urllib.request.Request(
            robots_txt_url, headers={"User-agent": DEFAULT_USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — intentional HTTP
            if resp.status == 404:
                return None
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _parse_robots(text: str) -> tuple[list[_RobotGroup], list[str]]:
    """Parse robots.txt text into rule groups + sitemap list (RFC 9309 §2.2)."""
    groups: list[_RobotGroup] = []
    current_ua: list[str] = []
    current_rules: list[_RobotRule] = []
    current_delay: float | None = None
    sitemaps: list[str] = []

    def _flush() -> None:
        if current_ua or current_rules:
            groups.append(
                _RobotGroup(list(current_ua), list(current_rules), current_delay)
            )

    for raw_line in text.splitlines():
        # Strip comments (§2.2: lines may end with '#...').
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, val = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        val = val.strip()
        if key == "user-agent":
            # A new user-agent record starts a new group (unless the previous
            # record is still empty — then append to it per §2.2 consecutive
            # User-agent lines).
            if val and not current_rules and not current_ua:
                current_ua.append(val.lower())
            else:
                _flush()
                current_ua = [val.lower()]
                current_rules = []
                current_delay = None
        elif key == "disallow":
            if val == "":
                continue  # empty Disallow = allow all (RFC 9309 §2.2.2)
            current_rules.append(_RobotRule(val, allow=False))
        elif key == "allow":
            if val == "":
                continue
            current_rules.append(_RobotRule(val, allow=True))
        elif key == "crawl-delay":
            try:
                current_delay = float(val)
            except ValueError:
                pass
        elif key == "sitemap":
            sitemaps.append(val)
        # `noindex` and other non-normative tokens are ignored (RFC 9309 §2.2).

    _flush()
    return groups, sitemaps


def _ua_matches(ua_list: list[str], target: str) -> bool:
    t = target.lower()
    return "*" in ua_list or t in ua_list


def _path_allowed(path: str, groups: list[_RobotGroup], user_agent: str) -> bool:
    """RFC 9309 §2.2.2 prefix matching.

    The most specific (longest) matching rule wins; on a length tie, an
    ``Allow`` beats a ``Disallow`` (common implementation choice, §O.2).
    When no rule matches, the path is allowed.
    """
    matched: list[tuple[bool, int]] = []
    for g in groups:
        if not _ua_matches(g.user_agents, user_agent):
            continue
        for rule in g.rules:
            if path.startswith(rule.path):
                matched.append((rule.allow, len(rule.path)))
    if not matched:
        return True
    max_len = max(length for _, length in matched)
    candidates = [allow for allow, length in matched if length == max_len]
    return any(candidates)  # allow wins on tie


class RobotsPolicy:
    """Minimal RFC 9309 robots.txt policy with an in-memory TTL cache.

    ``fetcher`` maps a ``robots.txt`` URL to its text (or ``None`` if absent).
    The default fetcher uses urllib over HTTP; tests inject a deterministic
    map or callable to avoid network.  Results are cached per-origin for
    ``cache_ttl`` seconds.
    """

    def __init__(
        self,
        fetcher: Callable[[str], str | None] | None = None,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        cache_ttl: float = 300.0,
    ) -> None:
        self._fetcher = fetcher or _default_fetcher
        self._user_agent = user_agent
        self._cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, str | None]] = {}

    def fetch_robots(self, origin_url: str) -> str | None:
        """Return (cached) robots.txt text for an origin, or ``None``."""
        now = time.monotonic()
        cached = self._cache.get(origin_url)
        if cached is not None and (now - cached[0]) < self._cache_ttl:
            return cached[1]
        text = self._fetcher(_robots_url(origin_url))
        self._cache[origin_url] = (now, text)
        return text

    def evaluate(self, url: str, user_agent: str | None = None) -> RobotsPolicyResult:
        """Evaluate RFC 9309 policy for *url*.

        Never treats the result as authorization — it gates *automated crawl*
        only.  Callers combine it with an explicit access basis / auth scope.
        """
        ua = user_agent or self._user_agent
        robots_text = self.fetch_robots(_origin(url))
        groups: list[_RobotGroup] = []
        sitemaps: list[str] = []
        rate_limit: float | None = None
        if robots_text is None:
            directive = RobotsDirective.NO_ROBOTS_TXT
            crawl_allowed = True
        else:
            groups, sitemaps = _parse_robots(robots_text)
            directive = (
                RobotsDirective.ALLOWED
                if _path_allowed(urlparse(url).path, groups, ua)
                else RobotsDirective.DISALLOWED
            )
            crawl_allowed = directive == RobotsDirective.ALLOWED
            for g in groups:
                if _ua_matches(g.user_agents, ua) and g.crawl_delay is not None:
                    rate_limit = g.crawl_delay
                    break
            if rate_limit is None:
                # Group-level '*' fallback.
                for g in groups:
                    if "*" in g.user_agents and g.crawl_delay is not None:
                        rate_limit = g.crawl_delay
                        break
        return RobotsPolicyResult(
            directive=directive,
            crawl_allowed=crawl_allowed,
            rate_limit=rate_limit,
            sitemaps=sitemaps,
        )


# ---------------------------------------------------------------------------
# Access-policy gate
# ---------------------------------------------------------------------------


@dataclass
class AccessPolicyResult:
    """Result of evaluating the §22 / ADR-015 gate for one URL.

    The fields map directly onto the ``source_identity`` staging columns:

    ``access_basis``   → source_identity.access_basis
    ``crawl_allowed``  → source_identity.crawl_allowed  (robots-policy *result*)
    ``robots_directive`` → (not a column) the RFC 9309 outcome
    ``auth_scope``     → source_identity.auth_scope
    ``license_spdx``   → source_identity.license_spdx
    ``redist_allowed`` → source_identity.redist_allowed

    Invariant: ``crawl_allowed=True`` is a *permission to attempt*, never
    authorization to redistribute or publish — that flows from
    ``access_basis`` + ``license_spdx`` + ``redist_allowed``.
    """

    access_basis: str
    crawl_allowed: bool
    robots_directive: RobotsDirective
    auth_scope: str | None = None
    license_spdx: str | None = None
    redist_allowed: bool | None = None
    rate_limit: float | None = None
    sitemaps: list[str] = field(default_factory=list)
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _default_license_resolver(url: str) -> str | None:
    """Best-effort license detection (no network; override per deployment).

    Real deployments replace this with a HEAD request + ``<link rel=license>``
    + copyright-sniffing.  It returns a best-effort SPDX identifier or ``None``.
    """
    return None


class AccessPolicyGate:
    """Enforce the §22 / ADR-015 acquisition policy for a URL.

    Combines the RFC 9309 robots result with a declared access basis and
    best-effort license detection.  The gate is pure-ish: it performs no DB
    writes — callers hand its result to ``InvestigatorContext.stage_*``.
    """

    def __init__(
        self,
        robots: RobotsPolicy | None = None,
        *,
        license_resolver: Callable[[str], str | None] | None = None,
        default_access_basis: SourceAccessBasis = SourceAccessBasis.PUBLIC,
    ) -> None:
        self._robots = robots or RobotsPolicy()
        self._license_resolver = license_resolver or _default_license_resolver
        self._default_access_basis = default_access_basis

    def evaluate(
        self,
        url: str,
        *,
        task_type: str | None = None,
        kind: str = "web",
        user_agent: str | None = None,
        access_basis: str | None = None,
        license_spdx: str | None = None,
        auth_scope: str | None = None,
        redist_allowed: bool | None = None,
    ) -> AccessPolicyResult:
        """Return the access-policy result for *url*.

        Explicitly passed fields override the defaults; omitted fields are
        derived (license via the resolver, access_basis via the default).
        """
        r = self._robots.evaluate(url, user_agent=user_agent)
        basis = access_basis or self._default_access_basis.value
        lic = license_spdx or self._license_resolver(url)

        # Default redistribution follows access basis, but is NEVER inferred
        # from the robots result alone (§22.2).
        if redist_allowed is None:
            redist_allowed = False
        reason = None
        if not r.crawl_allowed:
            reason = (
                f"RFC 9309 robots.txt disallows crawl for {url} "
                f"(directive={r.directive.value})"
            )
        return AccessPolicyResult(
            access_basis=basis,
            crawl_allowed=r.crawl_allowed,
            robots_directive=r.directive,
            auth_scope=auth_scope,
            license_spdx=lic,
            redist_allowed=redist_allowed,
            rate_limit=r.rate_limit,
            sitemaps=r.sitemaps,
            reason=reason,
            metadata={
                "task_type": task_type,
                "kind": kind,
                "robots_origin": _origin(url),
            },
        )
