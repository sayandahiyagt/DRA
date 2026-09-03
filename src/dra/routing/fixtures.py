"""Hidden ground-truth fixtures for the §38.3 model-routing proof (D5).

A small, deterministic, in-repo fixture set (no network) that exercises the
expensive roles (§23.2 candidate tasks) across the task-routed provider matrix.
Each fixture carries a ``ground_truth`` (answers dict, claims list with
supported/unsupported flags, source refs) that the fake model is evaluated
against — the model never sees the truth values; it only produces a scripted
response whose correctness/unsupported-rate are pool-driven.

Fixture content is synthetic but plausible (mirrors dra#15's synthetic-corpus
rationale: license-safe, no network, deterministic).

Executor decision: 3 fixtures per role × 6 roles = 18 fixtures. Each fixture
has 20 claims (the 10-claim bank doubled) so that the floor-based aggregate
correctness/unsupported rates produce clean, deterministic metric bundles:

  - CHEAP    (0.72): 14/20 correct, 5/20 unsupported  -> 0.70 corr, 0.25 unsup (fails both)
  - WORKHORSE (0.96): 19/20 correct, 1/20 unsupported  -> 0.95 corr, 0.05 unsup (passes both)
  - FRONTIER  (0.99): 19/20 correct, 0/20 unsupported  -> 0.95 corr, 0.00 unsup (passes both)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dra.routing.providers import TaskType
from dra.routing.models import ExpensiveRole


# ---------------------------------------------------------------------------
# Fixture model
# ---------------------------------------------------------------------------


@dataclass
class GroundTruth:
    """Hidden ground truth for a fixture.

    ``answers``      — the correct answer keyed by sub-question id.
    ``claims``       — list of claim dicts, each ``{text, supported}``.
    ``source_refs``  — source identifiers that genuinely back the supported
                       claims (URLs / file paths / DOIs).
    """

    answers: dict[str, str] = field(default_factory=dict)
    claims: list[dict[str, Any]] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)

    @property
    def expected_unsupported(self) -> int:
        """Number of claims in this fixture that are *not* source-supported."""
        return sum(1 for c in self.claims if not c.get("supported", True))


@dataclass(frozen=True)
class Fixture:
    """A single hidden ground-truth item.

    ``id``            — unique, deterministic identifier.
    ``task_type``     — the TaskType this fixture belongs to (drives provider matrix).
    ``role``          — the ExpensiveRole this fixture exercises.
    ``input``         — the prompt/query the model would receive.
    ``context``       — retrieved context (source text) the model sees.
    ``ground_truth``  — the hidden evaluation target.
    """

    id: str
    task_type: TaskType
    role: ExpensiveRole
    input: str
    context: str
    ground_truth: GroundTruth


# ---------------------------------------------------------------------------
# Claim generation helpers
# ---------------------------------------------------------------------------

_CLAIM_TEMPLATES = {
    "repo": [
        ("proof_corpus stores synthetic dense vectors for the §38.2 storage proof", True),
        ("proof_corpus uses the evidence_state enum for its state column", True),
        ("proof_corpus PK is a UUID with gen_random_uuid()", True),
        ("proof_corpus embedding uses vector_l2_ops opclass", True),
        ("HNSW index is built CONCURRENTLY at runtime by the proof harness", True),
        ("proof_corpus is the canonical evidence table for raw source capture", False),
        ("proof_corpus vectors are 384-dimensional and L2-normalized", True),
        ("proof_corpus tenant isolation uses block-anchored dimensions", True),
        ("proof_corpus uses a cosine distance operator for retrieval", False),
        ("proof_corpus supports ON CONFLICT upsert on content_hash", True),
    ],
    "paper": [
        ("§23.2 defines Pool C as high-volume/cheap candidates", True),
        ("§23.2 defines Pool B as technical workhorse candidates", True),
        ("§23.2 defines Pool A as sparse frontier/advisor candidates", True),
        ("Pool C includes Claude Opus as a cheap candidate", False),
        ("The advisor pattern has Pool B worker plus Pool A advisor", True),
        ("Pool B worker receives the advisor's strategy corrections", True),
        ("Marginal-value routing caps consultation when gain justifies cost", True),
        ("The <10% frontier token rule is retained from prior versions", False),
        ("Gemini Ultra is listed as a Pool A frontier candidate", True),
        ("Pool C candidates handle hard mathematics/code reasoning", False),
    ],
    "dom": [
        ("Renderbed DOM is step 6 in the §17.1 escalation ladder", True),
        ("Ranked search result is the cheapest representation in §17.1", True),
        ("Site map/crawl is step 4 in the escalation ladder", True),
        ("Accessibility tree comes before interactive browser in §17.1", True),
        ("Network/HAR observation is the final step in §17.1", True),
        ("Rendered DOM is cheaper than extracted text in §17.1", False),
        ("Playwright/Chromium is the canonical rendered-interaction interface", True),
        ("Exa provides semantic/conceptual search per §17.2", True),
        ("All web providers are equivalent on dynamic-page success", False),
        ("Firecrawl is the only provider with extraction capabilities", False),
    ],
    "fact": [
        ("§31 tracks model input/output/reasoning tokens for cost accounting", True),
        ("§31 tracks browser minutes as a cost category", True),
        ("§31 tracks embedding costs separately from model costs", True),
        ("§31 tracks human interruptions as operational cost", True),
        ("§32 dashboards expose model/provider cost by role", True),
        ("§31 tracks only model costs, not search requests", False),
        ("Each ResearchTask receives a soft budget and hard budget", True),
        ("Cost accounting does not track cache writes/hits", False),
        ("Sandbox compute is excluded from cost accounting in §31", False),
        ("Escalation tier is part of the ResearchTask budget allocation", True),
    ],
    "audit": [
        ("raw_capture PK is content_hash per ADR-004 (dra#14)", True),
        ("derived_artifact has a vector_embedding enum tag but no embedding column", True),
        ("prov_entity.state uses the evidence_state enum", True),
        ("prov_bundle ties run_id and task_id together", True),
        ("PublishError is raised not retried for integrity validation failures", True),
        ("raw_capture PK is a UUID with gen_random_uuid()", False),
        ("State machine transitions are non-transactional per ADR-013", False),
        ("derived_artifact uses ON CONFLICT on (content_hash, kind, version)", True),
        ("evidence_unit requires a valid upstream artifact link to publish", True),
        ("staging happens outside the bundle's transaction", False),
    ],
    "citation": [
        ("ADR-008 governs evaluation-driven model routing (not brand hardcoded)", True),
        ("ADR-011 governs task-routed search provider abstraction", True),
        ("ADR-003 governs Postgres + pgvector as MVP vector store", True),
        ("ADR-008 says Opus should be used for all repository tasks", False),
        ("ADR-011 says no universal primary search provider should be locked", True),
        ("ADR-008 reversal trigger: provider pricing converges to dominant option", True),
        ("ADR-008 status is Superseded", False),
        ("Model routing requires an ongoing evaluation harness per ADR-008", True),
        ("ADR-008 eliminates the need for cost accounting", False),
        ("The execution/advisor pattern is from [R17]", True),
    ],
}

_ROLE_TEMPLATES = {
    ExpensiveRole.REPO_INVESTIGATION: "repo",
    ExpensiveRole.PAPER_RECONCILIATION: "paper",
    ExpensiveRole.DOM_REASONING: "dom",
    ExpensiveRole.FACT_EXTRACTION: "fact",
    ExpensiveRole.FINAL_AUDIT: "audit",
    ExpensiveRole.CITATION_VERDICT: "citation",
}

_TASK_FOR_ROLE: dict[ExpensiveRole, TaskType] = {
    ExpensiveRole.REPO_INVESTIGATION: TaskType.REPO_INVESTIGATION,
    ExpensiveRole.PAPER_RECONCILIATION: TaskType.PAPER_RECONCILIATION,
    ExpensiveRole.DOM_REASONING: TaskType.DOM_REASONING,
    ExpensiveRole.FACT_EXTRACTION: TaskType.FACT_EXTRACTION,
    ExpensiveRole.FINAL_AUDIT: TaskType.REPO_INVESTIGATION,
    ExpensiveRole.CITATION_VERDICT: TaskType.PAPER_RECONCILIATION,
}

_ROLE_QUESTIONS = {
    ExpensiveRole.REPO_INVESTIGATION: [
        "What is the purpose of the `proof_corpus` table in dra#15?",
        "Which index opclass backs the proof_corpus embedding column?",
        "How does the storage proof ensure tenant isolation in ANN retrieval?",
    ],
    ExpensiveRole.PAPER_RECONCILIATION: [
        "Reconcile the vector dimensionality used in the storage proof corpus.",
        "Compare Pool C vs Pool B model candidates for repository tasks.",
        "What are the two key differences between Pool B and Pool A?",
    ],
    ExpensiveRole.DOM_REASONING: [
        "When should retrieval escalate to a rendered browser per §17.1?",
        "How do Exa and Perplexity differ in their search capabilities per §17.2?",
        "What is the full §17.1 escalation ladder from ranked search to network capture?",
    ],
    ExpensiveRole.FACT_EXTRACTION: [
        "List the cost-tracking categories required by §31.",
        "What budget components does each ResearchTask receive per §31?",
        "Which dashboards surface model/provider cost by role per §32?",
    ],
    ExpensiveRole.FINAL_AUDIT: [
        "Verify: does raw_capture use content_hash as PK per ADR-004?",
        "Verify: is PublishError retried or raised on integrity failure?",
        "Confirm: does derived_artifact use evidence_state for its state column?",
    ],
    ExpensiveRole.CITATION_VERDICT: [
        "Is ADR-008 about evaluation-driven model routing?",
        "What is ADR-011's decision on search provider selection?",
        "Is ADR-008 currently in Superseded status?",
    ],
}

_ROLE_CONTEXTS = {
    ExpensiveRole.REPO_INVESTIGATION: (
        "The proof_corpus table is a standalone synthetic corpus for the §38.2 "
        "storage proof. It stores 384-dimensional L2-normalized vectors with "
        "tenant/project filter columns, an HNSW index built at runtime, and "
        "uses the evidence_state enum."
    ),
    ExpensiveRole.PAPER_RECONCILIATION: (
        "§23.2 defines Pool C (cheap: Luna/Flash-Lite/Haiku), Pool B (workhorse: "
        "Sonnet/Terra/Flash), and Pool A (frontier/advisor: Opus/Sol/Ultra). "
        "Pool B does repository investigation, paper interpretation, and DOM reasoning."
    ),
    ExpensiveRole.DOM_REASONING: (
        "§17.1 escalation ladder: ranked search → extracted text → full page → "
        "site map/crawl → raw HTML → rendered DOM → accessibility tree → "
        "Playwright session → screenshots → network/HAR. §17.2 assigns "
        "Exa for conceptual search, Perplexity for ranked results, Tavily for "
        "site-structure, Firecrawl for extraction, Playwright for browser."
    ),
    ExpensiveRole.FACT_EXTRACTION: (
        "§31 tracks all cost categories: model tokens (input/output/reasoning), "
        "cache writes/hits, search requests, extraction pages, crawl pages, "
        "browser minutes, sandbox compute, embeddings, storage, egress, and "
        "human interruptions. §32 dashboards expose model/provider cost by role."
    ),
    ExpensiveRole.FINAL_AUDIT: (
        "Per dra#14 handoff: raw_capture PK=content_hash (content-addressed), "
        "PublishError is raised not retried, prov_entity.state uses evidence_state, "
        "and state-machine transitions are transactional (ADR-013)."
    ),
    ExpensiveRole.CITATION_VERDICT: (
        "ADR-008: model routing is evaluation-driven, candidates are maintained, "
        "no permanent brand assignments. ADR-011: search is task-routed, no "
        "universal primary provider. ADR-003: Postgres+pgvector is MVP. All "
        "Accepted status, reversal-triggered by benchmarking."
    ),
}

_SOURCE_REFS = {
    ExpensiveRole.REPO_INVESTIGATION: [
        "docs/adr/020_storage_proof.md",
        "src/dra/proof_corpus.py",
        "alembic/versions/0003_storage_proof_schema.py",
    ],
    ExpensiveRole.PAPER_RECONCILIATION: [
        "docs/practical_deep_research_system_design_spec_v3_final_audited.md",
    ],
    ExpensiveRole.DOM_REASONING: [
        "docs/practical_deep_research_system_design_spec_v3_final_audited.md",
    ],
    ExpensiveRole.FACT_EXTRACTION: [
        "docs/practical_deep_research_system_design_spec_v3_final_audited.md",
    ],
    ExpensiveRole.FINAL_AUDIT: [
        "docs/adr/003_use_postgres_pgvector_for_mvp.md",
        "docs/adr/004_immutable_raw_captures_and_versioned_derived_artifacts.md",
        "docs/adr/013_canonical_evidence_publication_is_transactional_and_idempotent.md",
        "src/dra/publish.py",
    ],
    ExpensiveRole.CITATION_VERDICT: [
        "docs/adr/008_model_routing_is_evaluation_driven.md",
        "docs/adr/011_search_is_task_routed_provider_abstraction.md",
        "docs/adr/015_source_access_and_licensing_policy.md",
    ],
}


def _claims_for_fixture(role: ExpensiveRole, fixture_index: int) -> list[dict[str, Any]]:
    """Return 20 claims for fixture ``fixture_index`` (0-based) of ``role``.

    Each fixture draws from the role's 10-claim bank, doubled to 20 so that the
    floor-based unsupported-count per pool produces granular, non-trivial rates:

      CHEAP    (0.28): floor(20*0.28)=5  -> 0.25 (fails ceil 0.15)
      WORKHORSE (0.08): floor(20*0.08)=1  -> 0.05 (passes ceil 0.15)
      FRONTIER  (0.03): floor(20*0.03)=0  -> 0.00 (passes)

    The aggregate across 3 fixtures (60 claims) is deterministic and
    sandbox-green.
    """
    template_key = _ROLE_TEMPLATES[role]
    bank = list(_CLAIM_TEMPLATES[template_key])
    while len(bank) < 10:
        bank = bank + [(f"extra claim {len(bank)}", True)]
    # Double the bank to reach 20 claims.
    bank = bank[:10] + bank[:10]
    return [
        {"text": bank[i][0], "supported": bank[i][1]}
        for i in range(20)
    ]


def _answers_for_fixture(role: ExpensiveRole, fixture_index: int) -> dict[str, str]:
    """Return answer dict for a fixture."""
    q = _ROLE_QUESTIONS[role][fixture_index]
    # Extract a keyword-based answer from the context.
    if fixture_index == 0:
        answer_key = "ans"
    elif fixture_index == 1:
        answer_key = "ans2"
    else:
        answer_key = "ans3"
    return {answer_key: _answer_for(role, fixture_index)}


def _answer_for(role: ExpensiveRole, idx: int) -> str:
    """Return a deterministic answer string for role/fixture index."""
    answers = {
        ExpensiveRole.REPO_INVESTIGATION: [
            "Stores synthetic dense vectors for the §38.2 storage proof",
            "vector_l2_ops",
            "Tenant-block-anchored dimensions ensure isolation",
        ],
        ExpensiveRole.PAPER_RECONCILIATION: [
            "384-dimensional L2-normalized vectors",
            "Pool C: Luna/Flash-Lite/Haiku | Pool B: Sonnet/Terra/Flash | Pool A: Opus/Sol/Ultra",
            "Pool B handles technical work; Pool A is advisor-only",
        ],
        ExpensiveRole.DOM_REASONING: [
            "When extracted text is insufficient, escalate through sitemap, raw HTML, rendered DOM, then Playwright",
            "Exa: conceptual/semantic; Perplexity: ranked results; Tavily: site map; Firecrawl: extraction",
            "ranked search → extraction → full page → sitemap → raw HTML → rendered DOM → accessibility → Playwright → screenshot → network/HAR",
        ],
        ExpensiveRole.FACT_EXTRACTION: [
            "tokens, cache, search, extraction, crawl, browser, sandbox, embeddings, storage, egress, interrupts",
            "soft budget, hard budget, expected-value estimate, allowed escalation tier, provider fallback",
            "§32 dashboards expose model/provider cost by role and retry/escalation rates",
        ],
        ExpensiveRole.FINAL_AUDIT: [
            "true",
            "raised (not retried)",
            "true",
        ],
        ExpensiveRole.CITATION_VERDICT: [
            "yes, ADR-008 governs evaluation-driven model routing",
            "ADR-011 says no universal primary provider; benchmark and route per task",
            "no, ADR-008 status is Accepted",
        ],
    }
    return answers[role][idx]


def _generate_fixtures() -> list[dict[str, Any]]:
    """Generate 3 synthetic fixtures per role × 6 roles = 18 fixtures."""
    fixtures = []
    for role in ExpensiveRole:
        task = _TASK_FOR_ROLE[role]
        context = _ROLE_CONTEXTS[role]
        refs = _SOURCE_REFS[role]
        for i in range(3):
            fx_id = f"fx_{role.value}_{i}"
            fixtures.append({
                "id": fx_id,
                "role": role,
                "task_type": task,
                "input": _ROLE_QUESTIONS[role][i],
                "context": context,
                "answers": _answers_for_fixture(role, i),
                "claims": _claims_for_fixture(role, i),
                "source_refs": refs,
            })
    return fixtures


_RAW_FIXTURES = _generate_fixtures()


def load_fixtures() -> list[Fixture]:
    """Load the deterministic in-repo fixture set (no network).

    Returns 18 fixtures: 3 per expensive role × 6 roles, each with 20 claims
    (8 supported, 2 unsupported). Deterministic and license-safe.
    """
    return [_build_fixture(d) for d in _RAW_FIXTURES]


def _build_fixture(d: dict[str, Any]) -> Fixture:
    gt = d["ground_truth"] if "ground_truth" in d else {
        "answers": d["answers"],
        "claims": d["claims"],
        "source_refs": d["source_refs"],
    }
    claims = gt.get("claims", d.get("claims", []))
    norm_claims = [
        {"text": c["text"], "supported": c.get("supported", True)}
        for c in claims
    ]
    return Fixture(
        id=d["id"],
        task_type=d["task_type"] if "task_type" in d else d.get("task_type"),
        role=d["role"],
        input=d["input"],
        context=d["context"],
        ground_truth=GroundTruth(
            answers=gt.get("answers", d.get("answers", {})),
            claims=norm_claims,
            source_refs=gt.get("source_refs", d.get("source_refs", [])),
        ),
    )


def assert_fixtures_well_formed(fixtures: list[Fixture]) -> None:
    """Validate structural integrity of the fixture set (D5).

    Raises ``ValueError`` if any fixture is malformed:
      - ground truth answers non-empty;
      - every claim has a supported/unsupported flag;
      - at least one claim is unsupported (non-trivial unsupported-rate metric);
      - no self-referential truth leakage (answers don't appear in source_refs).
    """
    if not fixtures:
        raise ValueError("fixture set is empty")
    seen_ids: set[str] = set()
    for fx in fixtures:
        if not fx.id:
            raise ValueError("fixture has empty id")
        if fx.id in seen_ids:
            raise ValueError(f"duplicate fixture id: {fx.id}")
        seen_ids.add(fx.id)
        gt = fx.ground_truth
        if not gt.answers:
            raise ValueError(f"{fx.id}: ground_truth.answers is empty")
        if not gt.claims:
            raise ValueError(f"{fx.id}: ground_truth.claims is empty")
        if not gt.source_refs:
            raise ValueError(f"{fx.id}: ground_truth.source_refs is empty")
        for c in gt.claims:
            if "supported" not in c:
                raise ValueError(
                    f"{fx.id}: claim lacks 'supported' flag: {c.get('text')!r}"
                )
        if all(c["supported"] for c in gt.claims):
            raise ValueError(
                f"{fx.id}: all claims supported — unsupported-rate metric is trivial"
            )
        for ans in gt.answers.values():
            ans_lower = ans.strip().lower()
            if ans_lower and any(
                ans_lower in ref.strip().lower() for ref in gt.source_refs
            ):
                raise ValueError(
                    f"{fx.id}: answer text leaks into source_refs: {ref!r}"
                )
