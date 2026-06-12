"""Minotaur Subnet 112 miner solver (king-01).

v2.3 — clean genesis baseline. The v2.2 QuoterV2 route override regressed
WETH-input swaps (it returned hop dicts the plan-builder couldn't encode when
the quoter picked a fee tier genesis hadn't discovered, causing on-chain
reverts) and timed out on cbBTC. We strip all overrides back to the OSS
``BaselineSwapSolver`` to re-establish the known-good ~0.62 baseline and read
the per-case feedback report, then re-add only surgical, measured fixes for
cases genesis itself fails — never regressing a case genesis already wins.
"""

from __future__ import annotations

import os

from strategies.dex_aggregator.baseline_solver import BaselineSwapSolver
from minotaur_subnet.sdk.intent_solver import SolverMetadata


SOLVER_NAME = os.environ.get("MINOTAUR_SOLVER_NAME", "king-01-solver")
SOLVER_VERSION = os.environ.get("MINOTAUR_SOLVER_VERSION", "2.3.0")
SOLVER_AUTHOR = os.environ.get("MINOTAUR_SOLVER_AUTHOR", "king-01")


class MinerSolver(BaselineSwapSolver):
    """Genesis baseline, unmodified routing. Metadata-only fork."""

    def metadata(self) -> SolverMetadata:
        base = super().metadata()
        return SolverMetadata(
            name=SOLVER_NAME,
            version=SOLVER_VERSION,
            author=SOLVER_AUTHOR,
            description="Genesis baseline (clean) — measurement run for per-case feedback",
            supported_chains=base.supported_chains,
            supported_intent_types=base.supported_intent_types,
        )


SOLVER_CLASS = MinerSolver
