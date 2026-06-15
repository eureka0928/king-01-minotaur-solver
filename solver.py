"""Minotaur Subnet 112 miner solver (king-01).

Resting state: clean genesis baseline. Rationale for sitting here rather than
shipping an override right now:

- The scoring system is in active flux. The self-quote fix (PR #181, citing
  issue #177) re-scored the genesis champion to ~0.833 by making each solver
  quote its own scenario with a 50%-loose min_output, which saturates the JS
  output term (ratio ≈ 2 ⇒ outputScore = 1.0) for any solver that executes.
- Under that scoring the only unsaturated levers are COVERAGE (execute the
  cases genesis still reverts on) and GAS. Output-quality routing barely moves
  the score, so the v2.x route/quote overrides have no edge — and v2.4 proved
  no solver-side change clears the cases that revert at the framework level.

We therefore hold at clean genesis (sane, never worse than baseline) until the
adoption pipeline certifies a challenger — the signal the rules are stable
enough to invest a focused coverage+gas push against. See
BENCHMARK_STALE_MINS_REPORT.md / issue #177 for the diagnostics that shaped
the fix.
"""

from __future__ import annotations

import os

from strategies.dex_aggregator.baseline_solver import BaselineSwapSolver
from minotaur_subnet.sdk.intent_solver import SolverMetadata


SOLVER_NAME = os.environ.get("MINOTAUR_SOLVER_NAME", "king-01-solver")
SOLVER_VERSION = os.environ.get("MINOTAUR_SOLVER_VERSION", "2.5.0")
SOLVER_AUTHOR = os.environ.get("MINOTAUR_SOLVER_AUTHOR", "king-01")


class MinerSolver(BaselineSwapSolver):
    """Genesis baseline, unmodified. Metadata-only fork (resting state)."""

    def metadata(self) -> SolverMetadata:
        base = super().metadata()
        return SolverMetadata(
            name=SOLVER_NAME,
            version=SOLVER_VERSION,
            author=SOLVER_AUTHOR,
            description="Genesis baseline (clean) — resting state pending scoring stabilization",
            supported_chains=base.supported_chains,
            supported_intent_types=base.supported_intent_types,
        )


SOLVER_CLASS = MinerSolver
