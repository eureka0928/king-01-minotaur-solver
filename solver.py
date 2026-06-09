"""Minotaur Subnet 112 miner solver (king-01).

Extends the OSS baseline with two narrow, defensible improvements:

1. **`compute_v3_output` bug fix (same-decimal pairs).** The shipped
   `strategies.dex_aggregator.pool_math.compute_v3_output` silently returns 0
   for 18/18-decimal token pairs (e.g. WETH/DAI) because its 1% sqrt-impact
   cap trips on the natively large sqrtPriceX96 values. We fall back to a
   price-based estimate when the upstream returns 0 on a healthy pool, so
   `find_best_pool` actually surfaces deep direct pools for DAI scenarios
   instead of pushing the router to a multi-hop that reverts on-chain.

2. **Gas-aware route substitution.** Genesis's `find_best_route` ranks
   purely by raw output, ignoring the DexAggregator scorer's 15% gas weight.
   When a multi-hop route's output advantage over a same-DEX single-hop is
   below ~3% per extra hop (the break-even point given the score weights),
   we substitute the single-hop. Cross-DEX substitutions are NOT performed
   (would change the plan-builder code path and risk reverts).

No quote sandbagging. We return genesis's honest `quote()` unchanged.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from strategies.dex_aggregator.baseline_solver import BaselineSwapSolver
from strategies.dex_aggregator.pool_math import (
    Q96,
    compute_v3_output,
    find_best_pool,
)
from minotaur_subnet.sdk.intent_solver import SolverMetadata

logger = logging.getLogger(__name__)


SOLVER_NAME = os.environ.get("MINOTAUR_SOLVER_NAME", "king-01-solver")
SOLVER_VERSION = os.environ.get("MINOTAUR_SOLVER_VERSION", "2.1.0")
SOLVER_AUTHOR = os.environ.get("MINOTAUR_SOLVER_AUTHOR", "king-01")

_MIN_GAIN_PER_EXTRA_HOP = 0.030


def _pool_dex(pool_state: dict[str, Any]) -> str:
    return pool_state.get("dex") or "uniswap_v3"


def _price_based_output(
    sqrt_price_x96: int, amount_in: int, zero_for_one: bool, fee_ppm: int,
) -> int:
    """No-slippage price-based output estimate (bug-fix fallback)."""
    if sqrt_price_x96 <= 0 or amount_in <= 0:
        return 0
    a = amount_in * (1_000_000 - fee_ppm) // 1_000_000
    if a <= 0:
        return 0
    if zero_for_one:
        return (a * sqrt_price_x96 // Q96) * sqrt_price_x96 // Q96
    return (a * Q96 // sqrt_price_x96) * Q96 // sqrt_price_x96


def _best_direct(
    pool_states: dict[str, dict[str, Any]],
    token_in: str, token_out: str, amount_in: int,
) -> tuple[str, dict[str, Any], int] | None:
    """Best direct pool by output, with the bug-fix fallback for same-decimal pairs."""
    tin, tout = token_in.lower(), token_out.lower()
    cands: list[tuple[str, dict[str, Any], int, bool, int, int]] = []
    max_liq = 0
    for addr, pool in pool_states.items():
        t0 = (pool.get("token0") or "").lower()
        t1 = (pool.get("token1") or "").lower()
        if t0 == tin and t1 == tout:
            z4o = True
        elif t0 == tout and t1 == tin:
            z4o = False
        else:
            continue
        liq = int(pool.get("liquidity") or 0)
        if liq <= 0:
            continue
        sqrt_price = int(pool.get("sqrtPriceX96") or 0)
        if sqrt_price <= 0:
            continue
        fee = int(pool.get("fee") or 3000)
        max_liq = max(max_liq, liq)
        cands.append((addr, pool, liq, z4o, fee, sqrt_price))
    if not cands:
        return None
    min_liq = max_liq // 20
    best = None
    for addr, pool, liq, z4o, fee, sqrt_price in cands:
        if liq < min_liq:
            continue
        try:
            out = compute_v3_output(sqrt_price, liq, amount_in, z4o, fee)
        except Exception:
            out = 0
        if out <= 0:
            out = _price_based_output(sqrt_price, amount_in, z4o, fee)
        if out <= 0:
            continue
        if best is None or out > best[2]:
            best = (addr, pool, out)
    return best


class MinerSolver(BaselineSwapSolver):
    """king-01 solver: baseline + same-decimal direct-pool recovery + gas-aware sub."""

    def _find_best_executable_route(
        self,
        pool_states: dict[str, dict[str, Any]],
        token_in: str, token_out: str, amount_in: int, chain_id: int,
    ) -> tuple[int, str, list[dict[str, Any]]] | None:
        result = super()._find_best_executable_route(
            pool_states, token_in, token_out, amount_in, chain_id,
        )

        if result is None:
            v3_pools = {a: p for a, p in pool_states.items()
                        if _pool_dex(p) == "uniswap_v3"}
            direct = _best_direct(v3_pools, token_in, token_out, amount_in)
            if direct is not None:
                addr, state, out = direct
                fee = int(state.get("fee", 3000))
                return (
                    out,
                    f"direct via {fee / 1_000_000:.2%} pool (decimal-fix recovery)",
                    [{"pool_addr": addr, "pool_state": state, "fee": fee}],
                )
            return None

        out, desc, hops = result
        if len(hops) <= 1:
            return result

        dominant_dex = _pool_dex(hops[0].get("pool_state", {}))
        if not all(_pool_dex(h.get("pool_state", {})) == dominant_dex for h in hops):
            return result
        same_dex = {a: p for a, p in pool_states.items()
                    if _pool_dex(p) == dominant_dex}
        direct = find_best_pool(same_dex, token_in, token_out, amount_in)
        if direct is None:
            direct = _best_direct(same_dex, token_in, token_out, amount_in)
        if direct is None:
            return result
        addr, state, direct_out = direct
        if direct_out <= 0:
            return result
        deficit_frac = (out - direct_out) / out
        required = _MIN_GAIN_PER_EXTRA_HOP * (len(hops) - 1)
        if deficit_frac >= required:
            return result
        fee = int(state.get("fee") or state.get("tickSpacing") or 3000)
        logger.info(
            "gas-aware substitute: %d-hop -> 1-hop %s (deficit=%.4f < %.4f)",
            len(hops), addr, deficit_frac, required,
        )
        return (
            direct_out,
            f"gas-aware direct via {fee / 1_000_000:.2%} {dominant_dex} pool",
            [{"pool_addr": addr, "pool_state": state, "fee": fee}],
        )

    def metadata(self) -> SolverMetadata:
        base = super().metadata()
        return SolverMetadata(
            name=SOLVER_NAME,
            version=SOLVER_VERSION,
            author=SOLVER_AUTHOR,
            description=(
                "Baseline + same-decimal direct-pool recovery (fixes "
                "compute_v3_output zero-return for 18/18-decimal pairs) + "
                "gas-aware single-hop substitution (3% per-hop threshold)"
            ),
            supported_chains=base.supported_chains,
            supported_intent_types=base.supported_intent_types,
        )


SOLVER_CLASS = MinerSolver
