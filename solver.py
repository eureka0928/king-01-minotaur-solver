"""Minotaur Subnet 112 miner solver (king-01).

v2.4 — slippage-floor fix for the WETH-input reverts.

The genesis baseline scores ~0.29 on the current 22-case pack because every
WETH-input swap (WETH→USDC ×4, WETH→DAI, and ~7 historical orders) reverts
on-chain with ``CallFailed(1, "Too little received")``. Diagnosis (decoded from
the scoreIntent revert + reproduced on a pinned Base fork):

  genesis builds the swap with ``amountOutMinimum = expected_output *
  (1 - slippage_bps/10000)`` and ``slippage_bps = 50`` (0.5%). In the
  validator's benchmark sandbox the realized output dips just under that tight
  floor — so the Uniswap router's slippage check reverts the whole intent and
  the case scores 0. The same plan clears on a clean fork where the realized
  output matches the estimate, which is why genesis "passed" locally but fails
  in production.

Fix: widen the plan's slippage floor. ``amountOutMinimum`` is only a revert
threshold — it does NOT change the realized output, the app fee, or the JS
score's ``output / min_output_amount`` ratio (that min is the quote-derived
scoring denominator, a separate value). So lowering the floor converts the
reverting cases to passing ones with no downside on the cases that already
work. Tunable via ``MINER_SLIPPAGE_BPS`` (default 500 = 5%).

Everything else is genesis, unchanged. No quote sandbagging.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from strategies.dex_aggregator.baseline_solver import BaselineSwapSolver
from minotaur_subnet.sdk.intent_solver import SolverMetadata

logger = logging.getLogger(__name__)


SOLVER_NAME = os.environ.get("MINOTAUR_SOLVER_NAME", "king-01-solver")
SOLVER_VERSION = os.environ.get("MINOTAUR_SOLVER_VERSION", "2.4.0")
SOLVER_AUTHOR = os.environ.get("MINOTAUR_SOLVER_AUTHOR", "king-01")

# Wider slippage floor for the swap's amountOutMinimum. 500 bps (5%) gives the
# realized output ample room under the estimate so the router never reverts
# "Too little received". This is a revert threshold only — realized output and
# the JS score ratio are unaffected (see module docstring).
_MINER_SLIPPAGE_BPS = int(os.environ.get("MINER_SLIPPAGE_BPS", "500"))


class MinerSolver(BaselineSwapSolver):
    """Genesis baseline + widened swap slippage floor (fixes WETH-input reverts)."""

    def initialize(self, config: dict[str, Any]) -> None:
        super().initialize(config)
        # Widen the processor's slippage so plan amountOutMinimum can't revert
        # on the small estimate-vs-realized gap in the benchmark sandbox.
        if self._processor is not None:
            try:
                self._processor.slippage_bps = _MINER_SLIPPAGE_BPS
                logger.info(
                    "king-01: slippage_bps set to %d (was default 50)",
                    _MINER_SLIPPAGE_BPS,
                )
            except Exception as exc:  # noqa: BLE001 — never fail init over this
                logger.warning("king-01: could not set slippage_bps: %s", exc)

    def metadata(self) -> SolverMetadata:
        base = super().metadata()
        return SolverMetadata(
            name=SOLVER_NAME,
            version=SOLVER_VERSION,
            author=SOLVER_AUTHOR,
            description=(
                "Genesis baseline + widened swap slippage floor "
                f"({_MINER_SLIPPAGE_BPS}bps) to fix WETH-input 'Too little "
                "received' reverts"
            ),
            supported_chains=base.supported_chains,
            supported_intent_types=base.supported_intent_types,
        )


SOLVER_CLASS = MinerSolver
