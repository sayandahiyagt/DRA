"""Shared fixture builder for the §38.2 storage proof tests.

Mirrors ``tests/_evidence.py``: provides a deterministic corpus generator
and a reset function that truncates ``proof_corpus``. Delegates to
``dra.proof_corpus.generate_corpus`` (deterministic, seed-pinned, no network).
"""

from __future__ import annotations

from tests._db import async_session
from dra.proof_corpus import generate_corpus, reset_corpus


async def reset() -> None:
    """Truncate the proof_corpus table."""
    await reset_corpus()


def build_corpus(
    rows: int = 5000,
    dim: int = 384,
    n_tenants: int = 3,
    projects_per_tenant: int = 2,
    topics_per_project: int = 5,
    seed: int = 42,
) -> list[dict]:
    """Build a deterministic synthetic corpus for proof tests."""
    return generate_corpus(
        n_vectors=rows,
        dim=dim,
        n_tenants=n_tenants,
        projects_per_tenant=projects_per_tenant,
        topics_per_project=topics_per_project,
        seed=seed,
    )
