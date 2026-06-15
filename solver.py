"""Minotaur Subnet 112 miner solver (king-01).

v3.1 — accurate quote of the EXACT route genesis chose, bounded and crash-safe.

Under the post-#181 self-quote rule the benchmark sets
``min_output = estimate * (1 - 50%)``, so the JS output term collapses to:

    outputScore = min(1.0, delivered / estimate)

i.e. the ONLY thing that matters for the (0.7-weighted) output term is that we
do not over-estimate. Genesis quotes with the single-tick ``compute_v3_output``
pool-math, which over-estimates on tick-crossing / illiquid orders — it delivers
as little as ~50% of its own estimate, so those cases score ~0.58 instead of
~0.92. Fix: re-price genesis's OWN chosen route with the real multi-tick
QuoterV2 and return a slightly conservative value, so ``delivered >= estimate``
and outputScore saturates at 1.0.

What changed v3.0 -> v3.1 (the v3.0 round crashed on WETH_to_USDC_xl with
"Solver process is not running", had_plan=false — the solver was KILLED during
quote):
  * v3.0 fanned out across ALL of a venue's tiers (4 Uni fees + 5 Aero
    tickSpacings = up to 9 eth_calls per quote). On the 5-WETH xl order the
    serial RPC fan-out blew the per-case quote budget and the worker was killed.
  * v3.1 re-prices ONLY the single tier genesis already chose, read straight
    from the quote metadata (``pools``/``fees``/``protocol``): 1 eth_call for a
    Uniswap hop, 2 for an Aerodrome hop (read ``tickSpacing()`` then quote). A
    wall-clock deadline bails to a conservative haircut before any budget blows.
  * This is also MORE correct: pricing the exact pool the plan executes
    guarantees ``estimate <= delivered``. v3.0's "max over all tiers" could pick
    a tier genesis does NOT execute, making estimate > delivered and LOWERING
    the score.
  * Dropped the v3.0 "direct-pool coverage" override entirely: it never improved
    a case (WETH/DAI's direct pool is too shallow; its guard always kept
    genesis) and it referenced an undefined ``_QUOTER_V2`` (latent NameError on
    any multi-hop / no-route case).

Routing is UNCHANGED — we re-price the exact route genesis already picked (same
pool/fee), so there is no plan divergence, no malformed hops, no coverage
regression. Multi-hop and any quoter failure fall to a conservative haircut of
genesis's own estimate. Never raises; never more than two view eth_calls.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from eth_abi import encode as _abi_encode

from strategies.dex_aggregator.baseline_solver import BaselineSwapSolver
from minotaur_subnet.sdk.intent_solver import SolverMetadata
from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    IntentState,
    QuoteResult,
)

logger = logging.getLogger(__name__)


SOLVER_NAME = os.environ.get("MINOTAUR_SOLVER_NAME", "king-01-solver")
SOLVER_VERSION = os.environ.get("MINOTAUR_SOLVER_VERSION", "3.1.0")
SOLVER_AUTHOR = os.environ.get("MINOTAUR_SOLVER_AUTHOR", "king-01")

# Uniswap V3 QuoterV2 (uint24 fee) + Aerodrome Slipstream QuoterV2 (int24
# tickSpacing). Both are view eth_calls; never send a tx. Base only.
_UNI_QUOTER = {8453: "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"}
_AERO_QUOTER = {8453: "0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0"}
_SEL_UNI_SINGLE = "c6a5026a"   # quoteExactInputSingle((address,address,uint256,uint24,uint160))
_SEL_AERO_SINGLE = "9e7defe6"  # quoteExactInputSingle((address,address,uint256,int24,uint160))
_SEL_TICK_SPACING = "d0c93a7c"  # tickSpacing() -> int24

# Slight conservative margin so realized output clears the returned estimate
# (outputScore = min(1, delivered/estimate) = 1.0). We price the EXACT pool the
# plan executes, so 1% absorbs ordinary block-to-block drift. The blind fallback
# (multi-hop / quoter down) haircuts genesis's possibly-inflated estimate harder.
_REPRICE_SAFETY = float(os.environ.get("KING_REPRICE_SAFETY", "0.99"))    # 1%
_BLIND_SAFETY = float(os.environ.get("KING_BLIND_SAFETY", "0.90"))        # 10%

# Hard wall-clock budget for the whole re-pricing step. With <=2 calls this is
# never hit in practice; it is the backstop that guarantees we bail to the
# conservative haircut rather than let a slow RPC get the worker killed (the
# v3.0 xl crash). Bounded call count is the primary guard; this is belt+braces.
_QUOTE_BUDGET_S = float(os.environ.get("KING_QUOTE_BUDGET_S", "6.0"))


class MinerSolver(BaselineSwapSolver):
    """Genesis baseline + QuoterV2-accurate, slightly-conservative quote.

    Only the ``estimated_output`` number is corrected; routing and plan
    generation are genesis's, untouched.
    """

    def _qcall(self, chain_id: int, to_addr: str, data_hex: str) -> int:
        """One view eth_call. Returns first 32 bytes as int, 0 on any failure."""
        if not to_addr:
            return 0
        try:
            w3 = self._get_web3(int(chain_id))
            if w3 is None:
                return 0
            raw = w3.eth.call({"to": to_addr, "data": "0x" + data_hex})
            return int.from_bytes(raw[:32], "big") if raw and len(raw) >= 32 else 0
        except Exception:
            return 0

    def _uni_single(self, chain_id, tin, tout, amt, fee) -> int:
        data = _SEL_UNI_SINGLE + _abi_encode(
            ["(address,address,uint256,uint24,uint160)"],
            [(tin, tout, int(amt), int(fee), 0)],
        ).hex()
        return self._qcall(chain_id, _UNI_QUOTER.get(int(chain_id), ""), data)

    def _aero_single(self, chain_id, tin, tout, amt, tick_spacing) -> int:
        data = _SEL_AERO_SINGLE + _abi_encode(
            ["(address,address,uint256,int24,uint160)"],
            [(tin, tout, int(amt), int(tick_spacing), 0)],
        ).hex()
        return self._qcall(chain_id, _AERO_QUOTER.get(int(chain_id), ""), data)

    def _read_tick_spacing(self, chain_id, pool_addr) -> int:
        """Read ``tickSpacing()`` (int24, always positive) off the chosen pool."""
        return self._qcall(chain_id, pool_addr, _SEL_TICK_SPACING)

    def _reprice_chosen_tier(
        self, chain_id, protocol, pools, fees, tin, tout, amt, deadline,
    ) -> int:
        """Accurate multi-tick output for the EXACT single-hop tier genesis chose.

        Reads the venue + tier from genesis's quote metadata so we re-price the
        same pool the plan executes (=> estimate <= delivered). 1 eth_call for
        Uniswap, 2 for Aerodrome. Returns 0 (=> blind haircut) on any miss or
        once the wall-clock deadline passes.
        """
        if time.monotonic() > deadline:
            return 0
        if "aerodrome" in protocol:
            if not pools:
                return 0
            ts = self._read_tick_spacing(chain_id, pools[0])
            if ts <= 0 or time.monotonic() > deadline:
                return 0
            return self._aero_single(chain_id, tin, tout, amt, ts)
        # uniswap / default: re-quote the exact fee tier genesis picked
        if not fees:
            return 0
        try:
            fee = int(fees[0])
        except (TypeError, ValueError):
            return 0
        if fee <= 0:
            return 0
        return self._uni_single(chain_id, tin, tout, amt, fee)

    def quote(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot=None,
    ) -> QuoteResult:
        # Genesis does the routing + builds the QuoteResult (route metadata,
        # platform fee, etc). We only correct the estimated_output number.
        q = super().quote(intent, state, snapshot)  # preserve genesis errors

        try:
            genesis_est = int(q.estimated_output or 0)
        except (TypeError, ValueError):
            return q
        if genesis_est <= 0:
            return q

        meta = q.metadata or {}
        hops = int(meta.get("hops") or 0)
        fees = meta.get("fees") or []
        pools = meta.get("pools") or []
        protocol = (meta.get("protocol") or "").lower()

        swap = self._normalized_swap_params(intent, state)
        token_in = swap.get("input_token", "")
        token_out = swap.get("output_token", "")
        try:
            amount_in = int(swap.get("input_amount", 0) or 0)
        except (TypeError, ValueError):
            amount_in = 0

        accurate = 0
        # Re-price genesis's chosen route with the real multi-tick quoter so the
        # estimate matches what the swap actually delivers (outputScore =
        # min(1, delivered/estimate)). Single-hop is priced exactly (the same
        # pool the plan executes); multi-hop falls to the blind haircut.
        if hops == 1 and amount_in > 0 and token_in and token_out:
            deadline = time.monotonic() + _QUOTE_BUDGET_S
            try:
                real = self._reprice_chosen_tier(
                    state.chain_id, protocol, pools, fees,
                    token_in, token_out, amount_in, deadline,
                )
            except Exception:
                real = 0  # never let re-pricing raise out of quote()
            if real > 0:
                accurate = int(real * _REPRICE_SAFETY)

        if accurate <= 0:
            # Multi-hop or quoter unavailable: blind conservative haircut so we
            # rarely over-estimate. Never raises the estimate above genesis's.
            accurate = int(genesis_est * _BLIND_SAFETY)

        # Only ever LOWER the estimate vs genesis (we must not promise more than
        # the route delivers; the plan executes genesis's route unchanged).
        new_est = min(genesis_est, accurate) if accurate > 0 else genesis_est
        if new_est <= 0 or new_est == genesis_est:
            return q

        logger.info(
            "king-01 quote: genesis_est=%d -> accurate=%d (hops=%d, proto=%s, ratio=%.3f)",
            genesis_est, new_est, hops, protocol or "?", new_est / genesis_est,
        )
        return QuoteResult(
            estimated_output=str(new_est),
            computed_params=dict(q.computed_params or {}),
            route_summary=q.route_summary,
            gas_estimate=q.gas_estimate,
            metadata={**meta, "king_repriced": True},
            platform_fee_wei=q.platform_fee_wei,
            platform_fee_token=q.platform_fee_token,
            platform_fee_symbol=q.platform_fee_symbol,
        )

    def metadata(self) -> SolverMetadata:
        base = super().metadata()
        return SolverMetadata(
            name=SOLVER_NAME,
            version=SOLVER_VERSION,
            author=SOLVER_AUTHOR,
            description=(
                "Genesis routing + QuoterV2-accurate conservative quote of the "
                "exact tier genesis chose (<=2 view eth_calls, budget-guarded) -> "
                "saturates outputScore without over-estimating"
            ),
            supported_chains=base.supported_chains,
            supported_intent_types=base.supported_intent_types,
        )


SOLVER_CLASS = MinerSolver
