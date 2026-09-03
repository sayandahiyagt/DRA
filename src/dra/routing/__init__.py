"""Provider-neutral, task-routed routing stack (dra#9, §38.3).

Public surface consumed by downstream missions:
  - ``providers``  — SearchProvider/ContentProvider/BrowserProvider protocols
    + SearchProviderRegistry → consumed by investigators (part 2) and the
    orchestrator (part 5) for per-source retrieval and fan-out.
  - ``models``    — ModelRegistry, ModelAdapter, ExpensiveRole, ModelPool
    → orchestrator (part 5) role-based dispatch + budget tiering.
  - ``policy``    — RoutingPolicy → shared cost/escalation decisions.
  - ``fixtures``  — fixture loader → evaluation harness for any benchmark cohort.
  - ``proof``     — the §38.3 proof harness (CLI: ``dra-model-routing-proof``).
"""

from dra.routing.providers import (
    BrowserProvider,
    ContentProvider,
    ProviderMode,
    SearchProvider,
    SearchProviderRegistry,
    SiteMapProvider,
    TaskType,
    make_providers,
)
from dra.routing.models import (
    ExpensiveRole,
    ModelAdapter,
    ModelPool,
    ModelRegistry,
    ModelSpec,
    model_pricing,
)
from dra.routing.fixtures import (
    Fixture,
    assert_fixtures_well_formed,
    load_fixtures,
)
from dra.routing.policy import (
    RoutingPolicy,
    compute_recall,
    compute_unsupported_rate,
    cost_of,
    latency_p95,
)

__all__ = [
    "BrowserProvider",
    "ContentProvider",
    "ProviderMode",
    "SearchProvider",
    "SearchProviderRegistry",
    "SiteMapProvider",
    "TaskType",
    "ExpensiveRole",
    "ModelAdapter",
    "ModelPool",
    "ModelRegistry",
    "ModelSpec",
    "model_pricing",
    "Fixture",
    "assert_fixtures_well_formed",
    "load_fixtures",
    "RoutingPolicy",
    "compute_recall",
    "compute_unsupported_rate",
    "cost_of",
    "latency_p95",
]
