"""Minotaur Subnet 112 miner solver (king-01).

v4.0 — accurate pricing of genesis's EXACT route, single- AND multi-hop.

Why this is the durable #1 design (not a gas/score trick):

The live JS scorer is
    finalScore = outputScore*0.7 + gasScore*0.15 + impactScore*0.15
    outputScore = min(1, delivered/estimate)            (under self-quote min)
    gasScore    = max(0, 1 - gasUsed/1e6)                (gasUsed = SIM actual)
    impactScore = max(0, 1 - |priceImpact|*20)

Genesis's own measurement: a 1-hop swap burns 508k-526k gas, of which ~400k is
FIXED app-harness overhead (ephemeral proxy deploy, EIP-712 verify, intent
wrapper, fee capture, delivery) that NO solver can avoid. So gasScore ~= 0.49 is
a universal hard ceiling, and the practical finalScore ceiling on liquid swaps
is ~0.92 for EVERYONE. You cannot win "top for a long time" by scoring higher on
the easy cases — nobody can. You win by leaving NO case where you score low and
a challenger scores high. There are exactly two such case classes, both about
not over-estimating:

  1. Over-estimate orders — genesis's single-tick pool-math over-quotes, so
     outputScore collapses (delivered/estimate ~= 0.5 -> case ~0.58). Accurate
     re-pricing pins estimate just under delivered -> outputScore = 1.0 (~0.92).
  2. Genesis-failed multi-hop (e.g. WETH->DAI) — genesis over-estimates the
     multi-hop, min_output is set too high, and the swap REVERTS -> 0.0. A
     challenger that prices it accurately scores ~0.92 and beats us on that case.

v3.1 fixed (1) for SINGLE-hop only; multi-hop fell to a crude blind haircut that
also bleeds score to the app's CoW fee (it skims a share of the surplus ABOVE
our quoted_output, so under-quoting hands money away). v4.0 extends accurate
pricing to MULTI-HOP: it re-derives genesis's exact route (pool discovery is
cached -> ~free) and prices every leg through the real multi-tick QuoterV2,
chaining current->next per hop. estimate = composed_output * 0.99, so
delivered >= estimate (outputScore = 1.0) AND the quote sits just under delivery
(near-zero fee skim). Routing is genesis's, untouched — we only correct the
estimate, so no plan divergence / malformed-hop reverts (the v2.2 failure).

Crash-safety carried from v3.1: bounded work (one quoter eth_call per leg; tier
comes from the cached pool_state, no extra reads), a wall-clock deadline that
bails to a conservative haircut, and a quote() that never raises out of the
re-pricing step.
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
SOLVER_VERSION = os.environ.get("MINOTAUR_SOLVER_VERSION", "4.0.0")
SOLVER_AUTHOR = os.environ.get("MINOTAUR_SOLVER_AUTHOR", "king-01")

# Uniswap V3 QuoterV2 (uint24 fee) + Aerodrome Slipstream QuoterV2 (int24
# tickSpacing). Both are view eth_calls; never send a tx. Base only.
_UNI_QUOTER = {8453: "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"}
_AERO_QUOTER = {8453: "0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0"}
_SEL_UNI_SINGLE = "c6a5026a"   # quoteExactInputSingle((address,address,uint256,uint24,uint160))
_SEL_AERO_SINGLE = "9e7defe6"  # quoteExactInputSingle((address,address,uint256,int24,uint160))

# Slight conservative margin so realized output clears the returned estimate
# (outputScore = min(1, delivered/estimate) = 1.0) while staying just under
# delivery to minimise the app's CoW fee skim. We price the EXACT pools the plan
# executes, so 1% absorbs ordinary block-to-block drift. The blind fallback
# (route re-derivation failed) haircuts genesis's possibly-inflated estimate.
_REPRICE_SAFETY = float(os.environ.get("KING_REPRICE_SAFETY", "0.99"))    # 1%
_BLIND_SAFETY = float(os.environ.get("KING_BLIND_SAFETY", "0.90"))        # 10%

# Hard wall-clock budget for the whole re-pricing step. With one call per leg
# this is never hit in practice; it is the backstop that guarantees we bail to
# the conservative haircut rather than let a slow RPC get the worker killed.
_QUOTE_BUDGET_S = float(os.environ.get("KING_QUOTE_BUDGET_S", "6.0"))

# Don't re-price absurdly long routes (keeps work bounded; genesis routes are
# 1-2 hops in practice). Longer routes fall back to the conservative haircut.
_MAX_REPRICE_HOPS = int(os.environ.get("KING_MAX_REPRICE_HOPS", "3"))


class MinerSolver(BaselineSwapSolver):
    """Genesis baseline + QuoterV2-accurate, slightly-conservative quote.

    Only ``estimated_output`` is corrected; routing and plan generation are
    genesis's, untouched.
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

    def _find_best_executable_route(
        self,
        pool_states: dict[str, dict[str, Any]],
        token_in: str, token_out: str, amount_in: int, chain_id: int,
    ):
        """Genesis's route finder, unchanged — we only STASH the result.

        super().quote() (and generate_plan()) already call this to pick the
        route; caching it here lets quote() re-price those exact hops without
        re-running (cached-but-not-free) pool discovery. No routing change.
        """
        route = super()._find_best_executable_route(
            pool_states, token_in, token_out, amount_in, chain_id,
        )
        try:
            self._king_last_route = {
                "key": (
                    (token_in or "").lower(), (token_out or "").lower(),
                    int(amount_in), int(chain_id),
                ),
                "hops": route[2] if route else None,
            }
        except Exception:
            self._king_last_route = None
        return route

    def _accurate_route_output(
        self, chain_id: int, hops: list[dict[str, Any]],
        token_in: str, token_out: str, amount_in: int, deadline: float,
    ) -> int:
        """Price genesis's exact route leg-by-leg via the real multi-tick quoter.

        Chains ``current -> next`` through each hop's pool (tier read from the
        already-discovered pool_state, so one quoter eth_call per leg, no extra
        reads). Returns the composed output, or 0 to fall back to the haircut.
        Single- and multi-hop are the same walk (1 leg vs N).
        """
        if not hops or len(hops) > _MAX_REPRICE_HOPS:
            return 0
        current = (token_in or "").lower()
        target = (token_out or "").lower()
        try:
            amt = int(amount_in)
        except (TypeError, ValueError):
            return 0

        for hop in hops:
            if amt <= 0 or time.monotonic() > deadline:
                return 0
            ps = hop.get("pool_state") or {}
            t0 = (ps.get("token0") or "").lower()
            t1 = (ps.get("token1") or "").lower()
            if not t0 or not t1:
                return 0
            if current == t0:
                nxt = t1
            elif current == t1:
                nxt = t0
            else:
                return 0  # route doesn't chain from the current token — bail

            dex = (ps.get("dex") or "uniswap_v3").lower()
            if "aerodrome" in dex:
                try:
                    ts = int(ps.get("tickSpacing") or 0)
                except (TypeError, ValueError):
                    return 0
                if ts <= 0:
                    return 0
                out = self._aero_single(chain_id, current, nxt, amt, ts)
            else:
                try:
                    fee = int(ps.get("fee") or hop.get("fee") or 0)
                except (TypeError, ValueError):
                    return 0
                if fee <= 0:
                    return 0
                out = self._uni_single(chain_id, current, nxt, amt, fee)

            if out <= 0:
                return 0
            amt = out
            current = nxt

        # Only trust a route that actually reaches the output token.
        if target and current != target:
            return 0
        return amt

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

        swap = self._normalized_swap_params(intent, state)
        token_in = swap.get("input_token", "")
        token_out = swap.get("output_token", "")
        try:
            amount_in = int(swap.get("input_amount", 0) or 0)
        except (TypeError, ValueError):
            amount_in = 0

        meta = q.metadata or {}
        hops_n = int(meta.get("hops") or 0)

        accurate = 0
        # Re-price genesis's chosen route (single- or multi-hop) with the real
        # multi-tick quoter so the estimate matches what the swap delivers
        # (outputScore = min(1, delivered/estimate) = 1.0) without over- or
        # under-quoting. Uses the hops super().quote() already routed (cached by
        # our _find_best_executable_route override — no extra discovery). Falls
        # to the blind haircut only if those hops are unavailable.
        if amount_in > 0 and token_in and token_out:
            try:
                last = getattr(self, "_king_last_route", None)
                want = (
                    (token_in or "").lower(), (token_out or "").lower(),
                    amount_in, int(state.chain_id),
                )
                hops = last.get("hops") if (last and last.get("key") == want) else None
                if hops:
                    deadline = time.monotonic() + _QUOTE_BUDGET_S
                    real = self._accurate_route_output(
                        state.chain_id, hops, token_in, token_out, amount_in, deadline,
                    )
                    if real > 0:
                        accurate = int(real * _REPRICE_SAFETY)
            except Exception:
                accurate = 0  # never let re-pricing raise out of quote()

        if accurate <= 0:
            # Route re-derivation unavailable: blind conservative haircut so we
            # rarely over-estimate. Never raises the estimate above genesis's.
            accurate = int(genesis_est * _BLIND_SAFETY)

        # Only ever LOWER the estimate vs genesis (we must not promise more than
        # the route delivers; the plan executes genesis's route unchanged).
        new_est = min(genesis_est, accurate) if accurate > 0 else genesis_est
        if new_est <= 0 or new_est == genesis_est:
            return q

        logger.info(
            "king-01 v4 quote: genesis_est=%d -> accurate=%d (hops=%d, ratio=%.3f)",
            genesis_est, new_est, hops_n, new_est / genesis_est,
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
                "exact route genesis chose, single- AND multi-hop (one view "
                "eth_call per leg, budget-guarded) -> outputScore=1.0 everywhere, "
                "rescues over-estimate reverts, minimal fee skim"
            ),
            supported_chains=base.supported_chains,
            supported_intent_types=base.supported_intent_types,
        )


SOLVER_CLASS = MinerSolver
