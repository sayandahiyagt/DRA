# §38.1 Control-Plane Bake-Off (non-canonical prototype)

> ⚠ NON-CANONICAL. This tree lives under `bake-off/` and is NOT part of the `dra`
> package (`[tool.setuptools.packages.find] where = ["src"]` excludes everything
> outside `src/`). Verified absent from the built wheel via the wheel smoke-check.
> It exists only to run the §38.1/§42 decision measurement and is gitignored from
> the package build (NOT from git history — see **Deviation from PLAN §6**).

Mission: `sayandandahiyagt/dra#37` — compare three control-plane substrates over
the **identical §2 workflow**: recon -> fan-out -> deep-investigation -> commit
-> verify -> synthesize, over a tiny deterministic local corpus, routing **every
finding through `dra.publish` / `publish_bundle`** so the dra.publish
evidence-graph bundle/commit contract stays the source of truth.

## Variants

| Variant | Substrate | Package | Native state measured |
|---|---|---|---|
| **A** — bare LangGraph | `langgraph` StateGraph + `AsyncPostgresSaver` | declared in `uv.lock` (langgraph==1.2.11, langgraph-checkpoint-postgres==3.1.2) | checkpoint holds control state only (`in_state_findings=0`) |
| **B** — LangGraph + Deep-Agents | `deepagents.create_deep_agent` + deterministic fake model | `deepagents==0.7.13` (NOT in `uv.lock`; lazy import, see below) | DeepAgents `files` channel empty (`in_state_findings=0`) |
| **C** — DeerFlow-derived | vendored DeerFlow 2 `create_deerflow_agent` | `git clone --branch v2.0.0` (no PyPI package) | DeerFlow `ThreadState.thread_data` populated (`in_state_findings>0` -> **DISQUALIFIED**) |

## Run

```bash
# prerequisites: Postgres+pgvector at $DATABASE_URL (default host.docker.internal:5432)
uv sync
python bake-off/measure.py                 # run all three + write results.json/results.md
python bake-off/variant_a_langgraph/run.py # one variant
python bake-off/variant_b_deep_agents/worker.py
DEER_FLOW_CONFIG_PATH=.../.../deerflow/config.yaml python bake-off/variant_c_deerflow/run.py
pytest bake-off/tests/test_all_variants.py
```

The bake-off uses a **deterministic no-LLM fake model** (`lifecycle_tools.py`) so
no real API calls / network are made — the comparison is about orchestration
substrate characteristics, not model quality.

## Decision rule (§38.1/§42)

> LangGraph remains the control-plane substrate unless an alternative
> **materially reduces measured cost (>=20% lower composite) AND does not force
> canonical evidence into agent-internal state** (`in_state_findings==0`).

Result: **A chosen; C disqualified.** C is disqualified because DeerFlow's native
`ThreadState` materialises tool results into agent-internal `thread_data`
(`ThreadDataMiddleware`, `sandbox=True`), so canonical evidence is not held
exclusively on `dra.publish` — regardless of raw score.

## Deviation from PLAN_1.md (smallest sound, documented)

1. **No `bakeoff-deep` extra in `pyproject.toml`/`uv.lock`** (PLAN §3b): adding it
   perturbed canonical pins (`pypdfium2` reformat, etc.). Per PLAN §8 ("no promotion
   into uv.lock for non-bake-off use"), Variant B's `deepagents` is installed via
   `uv pip install` into the sandbox venv (NOT the lock); non-bake-off installs
   (`uv sync`) are unaffected. The lazy import in `variant_b_deep_agents/worker.py`
   raises a clear error if absent.
2. **`AsyncPostgresSaver` API** (PLAN §3a): langgraph-checkpoint-postgres 3.1.2 has
   no `PostgresSaver.from_engine(engine, schema)`; used
   `AsyncPostgresSaver.from_conn_string(postgres_conninfo(DATABASE_URL)) + .setup()`
   (mirrors `src/dra/control_plane.py`).
3. **DeerFlow shim** (PLAN §3c): in `variant_c_deerflow/adapter.py`, a lockfile-neutral
   runtime-context shim wraps `ThreadDataMiddleware.before_agent` so `sandbox=True`
   runs (langgraph's `Runtime.context` is `None` for `create_agent`); no DeerFlow
   dependency is added to `uv.lock`.
4. **`bake-off/` committed, not gitignored** (PLAN §6): gitignoring it caused the
   round-2 REVIEW reject (gitignored deliverables vanish across the workspace
   reset and are absent from the reviewed checkout). Since packaging uses
   `where=["src"]`, committing `bake-off/` does NOT pollute the wheel (verified by
   the wheel smoke-check). Only the heavy regenerable DeerFlow **clone**
   (`bake-off/variant_c_deerflow/deerflow/`) stays gitignored.
5. **`deep-agents` package name** (finding §1.2): the mission's literal `deep-agents`
   is 404 on PyPI; the resolvable name is `deepagents`.

## Environment note

This run executed against a pre-provisioned Postgres 16 + pgvector on
`host.docker.internal:5432` (the environment's `DATABASE_URL`). The DB-gated
tests `SKIP` cleanly when Postgres is unreachable (spec §21: env concern, not a
code defect). Variant B requires `deepagents`+`langchain` (installed via `uv pip`,
not in `uv.lock`); Variant C requires the vendored DeerFlow clone. `measure.py`
preserves committed real numbers for any variant whose optional deps are absent.

## Deliverables

- `bake-off/results.json` / `bake-off/results.md` — the 8-dimension measurement
  table + §38.1/§42 recommendation + commit-bundle evidence note.
- `bake-off/tests/` — DB-gated invariant tests.
- `bake-off/workflow_def.py` — the identical §2 lifecycle definition.
