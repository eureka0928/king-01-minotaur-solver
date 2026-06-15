"""Minotaur Subnet 112 miner solver (king-01).

v3.0 — accurate quote, the durable lever under self-quote scoring.

Under the post-#181 self-quote rule the benchmark sets
``min_output = estimate * (1 - 50%)``, so the JS output term collapses to:

    outputScore = min(1.0, delivered / estimate)

i.e. the ONLY thing that matters for the (0.7-weighted) output term is that we
do not over-estimate. Genesis quotes with the single-tick ``compute_v3_output``
pool-math, which over-estimates on tick-crossing / illiquid orders — it delivers
as little as ~50% of its own estimate, so those cases score ~0.58 instead of
~0.92. Fix: re-price genesis's OWN chosen route with the real Uniswap V3
QuoterV2 (multi-tick, exact) and return a slightly conservative value, so
``delivered >= estimate`` and outputScore saturates at 1.0.

Why this is safe and durable:
  * Routing is UNCHANGED — we re-price the exact route genesis already picked
    (same pools/fees), so there is no plan divergence, no malformed hops, and no
    coverage regression. (v2.2 broke this by substituting routes.)
  * It helps WHATEVER orders a round samples, not pack-specific tuning.
  * QuoterV2 is one view-only eth_call for the chosen single-hop tier; on any
    failure or for multi-hop we fall back to a conservative haircut of genesis's
    own estimate. Never slower than a couple of calls, never raises.

No quote sandbagging beyond the small safety margin needed to guarantee
``delivered >= estimate`` (quoted_output stays ≈ delivered ⇒ CoW fee ≈ 0).
"""

from __future__ import annotations

import logging
import os
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
SOLVER_VERSION = os.environ.get("MINOTAUR_SOLVER_VERSION", "3.0.0")
SOLVER_AUTHOR = os.environ.get("MINOTAUR_SOLVER_AUTHOR", "king-01")

# Uniswap V3 QuoterV2 (uint24 fee) + Aerodrome Slipstream QuoterV2 (int24
# tickSpacing). Both are view eth_calls; never send a tx. Base only.
_UNI_QUOTER = {8453: "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"}
_AERO_QUOTER = {8453: "0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0"}
_SEL_UNI_SINGLE = "c6a5026a"   # quoteExactInputSingle((address,address,uint256,uint24,uint160))
_SEL_AERO_SINGLE = "9e7defe6"  # quoteExactInputSingle((address,address,uint256,int24,uint160))

_UNI_FEES = (100, 500, 3000, 10000)
_AERO_TICK_SPACINGS = (1, 50, 100, 200, 2000)

# Slight conservative margin so realized output clears the returned estimate
# (outputScore = min(1, delivered/estimate) = 1.0). The chosen venue's best
# single-hop ≈ what genesis's single-hop plan delivers, so 1% is ample. The
# blind fallback (multi-hop / quoter down) haircuts genesis's possibly-inflated
# estimate harder since we can't price the exact route.
_REPRICE_SAFETY = float(os.environ.get("KING_REPRICE_SAFETY", "0.99"))    # 1%
_BLIND_SAFETY = float(os.environ.get("KING_BLIND_SAFETY", "0.90"))        # 10%


class MinerSolver(BaselineSwapSolver):
    """Genesis baseline + QuoterV2-accurate, slightly-conservative quote."""

    def _qcall(self, chain_id: int, quoter: str, data_hex: str) -> int:
        """One quoter eth_call. Returns first 32 bytes as int, 0 on any failure."""
        if not quoter:
            return 0
        try:
            w3 = self._get_web3(int(chain_id))
            if w3 is None:
                return 0
            raw = w3.eth.call({"to": quoter, "data": "0x" + data_hex})
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

    def _best_single_on_venue(
        self, chain_id: int, venue: str, tin: str, tout: str, amt: int,
    ) -> int:
        """Accurate best single-hop output on the given venue (real quoter).

        Genesis picks the best tier on its chosen venue, so the max over that
        venue's tiers ≈ what genesis's single-hop plan delivers — which makes
        ``estimate = that * 0.99 <= delivered`` and saturates outputScore.
        """
        best = 0
        if "aerodrome" in venue:
            for ts in _AERO_TICK_SPACINGS:
                out = self._aero_single(chain_id, tin, tout, amt, ts)
                if out > best:
                    best = out
        else:  # uniswap / default
            for fee in _UNI_FEES:
                out = self._uni_single(chain_id, tin, tout, amt, fee)
                if out > best:
                    best = out
        return best

    def _quote_single_exact(self, chain_id, tin, tout, amt, fee) -> int:
        """Back-compat helper used by the coverage path (Uniswap direct)."""
        return self._uni_single(chain_id, tin, tout, amt, fee)

    def quote(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot=None,
    ) -> QuoteResult:
        # Genesis does the routing + builds the QuoteResult (route metadata,
        # platform fee, etc). We only correct the estimated_output number.
        try:
            q = super().quote(intent, state, snapshot)
        except Exception:
            raise  # preserve genesis's error semantics

        try:
            genesis_est = int(q.estimated_output or 0)
        except (TypeError, ValueError):
            return q
        if genesis_est <= 0:
            return q

        meta = q.metadata or {}
        hops = int(meta.get("hops") or 0)
        fees = meta.get("fees") or []
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
        # min(1, delivered/estimate)). Single-hop on either venue is priced
        # exactly; multi-hop falls to the blind haircut. This corrects genesis's
        # single-tick over-estimate on thin/large orders — the differentiator.
        if (
            hops == 1 and amount_in > 0
            and token_in and token_out
        ):
            real = self._best_single_on_venue(
                state.chain_id, protocol, token_in, token_out, amount_in,
            )
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
            "king-01 quote: genesis_est=%d -> accurate=%d (hops=%d, ratio=%.3f)",
            genesis_est, new_est, hops, new_est / genesis_est,
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

    # ── coverage: QuoterV2-verified direct single-hop ───────────────────────
    @staticmethod
    def _find_uni_pool(
        pool_states: dict[str, dict[str, Any]], token_in: str, token_out: str, fee: int,
    ) -> tuple[str, dict[str, Any]] | None:
        tin, tout = token_in.lower(), token_out.lower()
        for addr, p in pool_states.items():
            if (p.get("dex") or "uniswap_v3") != "uniswap_v3":
                continue
            if int(p.get("fee") or 0) != int(fee):
                continue
            t0 = (p.get("token0") or "").lower()
            t1 = (p.get("token1") or "").lower()
            if {t0, t1} == {tin, tout}:
                return addr, p
        return None

    def _find_best_executable_route(
        self,
        pool_states: dict[str, dict[str, Any]],
        token_in: str, token_out: str, amount_in: int, chain_id: int,
    ):
        genesis = super()._find_best_executable_route(
            pool_states, token_in, token_out, amount_in, chain_id,
        )

        # Coverage fix: when genesis has no route or a MULTI-HOP one (which it
        # picks for same-decimal pairs like WETH/DAI because its single-tick
        # pool-math zeroes the direct pool, then reverts on-chain), prefer a
        # direct Uniswap V3 single-hop that the real QuoterV2 confirms executes.
        # Multi-hop genesis routes that already work are left untouched (we only
        # override when genesis is None or multi-hop AND a verified direct exists).
        needs_help = genesis is None or (genesis is not None and len(genesis[2]) > 1)
        if not needs_help or amount_in <= 0:
            return genesis
        if not _QUOTER_V2.get(int(chain_id)):
            return genesis

        best_out, best_fee, best_pool = 0, 0, None
        for fee in (100, 500, 3000, 10000):
            pool = self._find_uni_pool(pool_states, token_in, token_out, fee)
            if pool is None:
                continue
            out = self._quote_single_exact(chain_id, token_in, token_out, amount_in, fee)
            if out > best_out:
                best_out, best_fee, best_pool = out, fee, pool

        if best_pool is None or best_out <= 0:
            return genesis  # no verified direct pool — keep genesis

        # If genesis is a working multi-hop that delivers MORE than the direct,
        # keep it (output saturates anyway, but never regress delivery). Only
        # override a multi-hop when genesis is None or the direct is comparable.
        if genesis is not None and len(genesis[2]) > 1:
            try:
                if int(genesis[0]) > 0 and best_out < int(genesis[0]) * 0.9:
                    return genesis
            except (TypeError, ValueError, IndexError):
                pass

        addr, state = best_pool
        logger.info(
            "king-01 coverage: direct UniV3 %s fee=%d out=%d (genesis=%s hops)",
            addr, best_fee, best_out,
            "none" if genesis is None else len(genesis[2]),
        )
        return (
            best_out,
            f"king direct UniV3 {best_fee / 1_000_000:.2%}",
            [{"pool_addr": addr, "pool_state": dict(state), "fee": best_fee}],
        )

    def metadata(self) -> SolverMetadata:
        base = super().metadata()
        return SolverMetadata(
            name=SOLVER_NAME,
            version=SOLVER_VERSION,
            author=SOLVER_AUTHOR,
            description=(
                "Genesis + QuoterV2-accurate conservative quote + direct-pool "
                "coverage for same-decimal pairs (saturates outputScore, fixes "
                "WETH/DAI-style multi-hop reverts)"
            ),
            supported_chains=base.supported_chains,
            supported_intent_types=base.supported_intent_types,
        )


SOLVER_CLASS = MinerSolver
