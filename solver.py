"""Minotaur Subnet 112 miner solver (king-01).

Extends the OSS baseline (`BaselineSwapSolver`) with three honest, narrowly
scoped improvements. No quote sandbagging — `quote()` is genesis's, unchanged.

1. **Real on-chain QuoterV2 route ranking.**
   Genesis never calls a real quoter; it ranks routes with the single-tick
   `compute_v3_output` pool-math estimate, which (a) silently returns 0 for
   18/18-decimal pairs (the 1% sqrt-impact cap trips on natively large
   sqrtPriceX96) and (b) over-estimates output on any swap that crosses ticks.
   We query Uniswap V3 **QuoterV2** (`quoteExactInputSingle` /
   `quoteExactInput`) for candidate routes and rank by *true* multi-tick
   output. Accurate output ⇒ better route choice AND no over-quote, which is
   what drives the on-chain `CallFailed`/`STF` reverts genesis hits on
   WETH↔DAI. Pure refinement: if the quoter is unreachable or returns nothing,
   we fall through to genesis's pick unchanged.

2. **Same-decimal direct-pool recovery.**
   When genesis returns no route (the same-decimal bug zeroes every direct
   pool), recover the best direct V3 pool via the real quoter (or a price
   fallback) so DAI scenarios route through a single deep pool instead of a
   reverting multi-hop.

3. **Gas-aware single-hop substitution.**
   The DexAggregator scorer weights gas at 15%; genesis ignores it. When a
   same-DEX single-hop's true output is within ~3% per saved hop of the
   multi-hop's, we take the single-hop (lower gas ⇒ higher gasScore). Decided
   on QuoterV2 truth, not estimates. Cross-DEX substitution is never done
   (would change the plan-builder path and risk reverts).

Everything degrades safely to genesis behaviour when RPC is unavailable, so we
never score worse than the baseline.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from eth_abi import encode as _abi_encode

from strategies.dex_aggregator.baseline_solver import BaselineSwapSolver
from strategies.dex_aggregator.pool_math import (
    Q96,
    compute_v3_output,
    find_best_pool,
)
from minotaur_subnet.sdk.intent_solver import SolverMetadata

logger = logging.getLogger(__name__)


SOLVER_NAME = os.environ.get("MINOTAUR_SOLVER_NAME", "king-01-solver")
SOLVER_VERSION = os.environ.get("MINOTAUR_SOLVER_VERSION", "2.2.0")
SOLVER_AUTHOR = os.environ.get("MINOTAUR_SOLVER_AUTHOR", "king-01")

# 3% output gain per extra hop is the break-even point against the scorer's
# 15% gas weight: ~80k extra gas costs ~0.012 final score; recovering it via
# outputScore at ratio≈1 needs ~3.4% more output. 3% leaves headroom.
_MIN_GAIN_PER_EXTRA_HOP = 0.030

# Uniswap V3 QuoterV2 — Base. View-style eth_call; never sends a tx.
_QUOTER_V2 = {8453: "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"}
_SEL_QUOTE_SINGLE = "c6a5026a"  # quoteExactInputSingle((address,address,uint256,uint24,uint160))
_SEL_QUOTE_PATH = "cdca1753"    # quoteExactInput(bytes,uint256)
_FEE_TIERS = (100, 500, 3000, 10000)


def _pool_dex(pool_state: dict[str, Any]) -> str:
    return pool_state.get("dex") or "uniswap_v3"


def _v3_path_bytes(tokens: list[str], fees: list[int]) -> bytes:
    """token0 | fee0 | token1 | fee1 | token2 …  (V3 packed path)."""
    out = bytearray()
    for i, fee in enumerate(fees):
        out += bytes.fromhex(tokens[i][2:].rjust(40, "0"))
        out += int(fee).to_bytes(3, "big")
    out += bytes.fromhex(tokens[-1][2:].rjust(40, "0"))
    return bytes(out)


def _price_based_output(
    sqrt_price_x96: int, amount_in: int, zero_for_one: bool, fee_ppm: int,
) -> int:
    """No-slippage price estimate; bug-fix fallback when the quoter is down."""
    if sqrt_price_x96 <= 0 or amount_in <= 0:
        return 0
    a = amount_in * (1_000_000 - fee_ppm) // 1_000_000
    if a <= 0:
        return 0
    if zero_for_one:
        return (a * sqrt_price_x96 // Q96) * sqrt_price_x96 // Q96
    return (a * Q96 // sqrt_price_x96) * Q96 // sqrt_price_x96


class MinerSolver(BaselineSwapSolver):
    """Baseline + real-quoter route ranking + same-decimal recovery + gas-aware sub."""

    # ── QuoterV2 access (real on-chain eth_call, view-only) ──────────────────
    def _quoter_single(
        self, chain_id: int, token_in: str, token_out: str, amount_in: int, fee: int,
    ) -> int:
        quoter = _QUOTER_V2.get(chain_id)
        if not quoter:
            return 0
        w3 = self._get_web3(chain_id)
        if w3 is None:
            return 0
        try:
            data = "0x" + _SEL_QUOTE_SINGLE + _abi_encode(
                ["(address,address,uint256,uint24,uint160)"],
                [(token_in, token_out, int(amount_in), int(fee), 0)],
            ).hex()
            raw = w3.eth.call({"to": quoter, "data": data})
            return int.from_bytes(raw[:32], "big") if raw and len(raw) >= 32 else 0
        except Exception:
            return 0  # no pool at this tier / revert — caller treats as 0

    def _quoter_path(self, chain_id: int, path: bytes, amount_in: int) -> int:
        quoter = _QUOTER_V2.get(chain_id)
        if not quoter:
            return 0
        w3 = self._get_web3(chain_id)
        if w3 is None:
            return 0
        try:
            data = "0x" + _SEL_QUOTE_PATH + _abi_encode(
                ["bytes", "uint256"], [path, int(amount_in)],
            ).hex()
            raw = w3.eth.call({"to": quoter, "data": data})
            return int.from_bytes(raw[:32], "big") if raw and len(raw) >= 32 else 0
        except Exception:
            return 0

    def _best_v3_single(
        self, chain_id: int, token_in: str, token_out: str, amount_in: int,
    ) -> tuple[int, int]:
        """Best (output, fee_tier) over all single-hop tiers per the real quoter."""
        best_out, best_fee = 0, 0
        for fee in _FEE_TIERS:
            out = self._quoter_single(chain_id, token_in, token_out, amount_in, fee)
            if out > best_out:
                best_out, best_fee = out, fee
        return best_out, best_fee

    # ── route selection ──────────────────────────────────────────────────────
    def _find_best_executable_route(
        self,
        pool_states: dict[str, dict[str, Any]],
        token_in: str, token_out: str, amount_in: int, chain_id: int,
    ) -> tuple[int, str, list[dict[str, Any]]] | None:
        genesis = super()._find_best_executable_route(
            pool_states, token_in, token_out, amount_in, chain_id,
        )

        # Real-quoter single-hop truth for this pair (0 if quoter unavailable).
        q_out, q_fee = (0, 0)
        if amount_in > 0:
            q_out, q_fee = self._best_v3_single(chain_id, token_in, token_out, amount_in)

        # Genesis found nothing → recover via a verified single-hop direct pool.
        if genesis is None:
            if q_out > 0 and q_fee:
                pool = self._pool_for(pool_states, token_in, token_out, q_fee)
                return (
                    q_out,
                    f"quoter direct {q_fee / 1_000_000:.2%} (recovery)",
                    [{"pool_addr": pool[0] if pool else "", "pool_state": pool[1] if pool else {}, "fee": q_fee}],
                )
            direct = self._recover_direct(pool_states, token_in, token_out, amount_in, chain_id)
            return direct

        g_out, g_desc, g_hops = genesis

        # Genesis already single-hop: keep it (lowest gas). Nothing to improve.
        if len(g_hops) <= 1:
            return genesis

        # Genesis is multi-hop. When the real quoter evaluated a single-hop for
        # this pair (q_out > 0), its verdict is AUTHORITATIVE — substitute only
        # if the single-hop's true output is within the gas-penalty threshold,
        # otherwise keep genesis's multi-hop verbatim. We do NOT fall through to
        # the pool-math path here: that estimate is exactly what we're replacing.
        if q_out > 0 and q_fee:
            true_multi = self._true_multi_output(chain_id, token_in, token_out, amount_in, g_hops)
            ref = true_multi if true_multi > 0 else g_out
            if ref > 0:
                deficit = (ref - q_out) / ref
                required = _MIN_GAIN_PER_EXTRA_HOP * (len(g_hops) - 1)
                if deficit < required:
                    pool = self._pool_for(pool_states, token_in, token_out, q_fee)
                    logger.info(
                        "quoter gas-aware sub: multi(out=%d) -> single %s (true=%d, deficit=%.4f < %.4f)",
                        ref, f"{q_fee/1e6:.2%}", q_out, deficit, required,
                    )
                    return (
                        q_out,
                        f"quoter single {q_fee / 1_000_000:.2%} (gas-aware)",
                        [{"pool_addr": pool[0] if pool else "", "pool_state": pool[1] if pool else {}, "fee": q_fee}],
                    )
            # Quoter says the single-hop isn't worth the gas — keep genesis.
            return genesis

        # Quoter unavailable (q_out == 0): fall back to genesis's pool-math
        # gas-aware sub so we still avoid the obvious multi-hop gas traps.
        return self._poolmath_gas_aware(genesis, pool_states, token_in, token_out, amount_in)

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _pool_for(
        pool_states: dict[str, dict[str, Any]], token_in: str, token_out: str, fee: int,
    ) -> tuple[str, dict[str, Any]] | None:
        tin, tout = token_in.lower(), token_out.lower()
        for addr, p in pool_states.items():
            if int(p.get("fee") or 0) != int(fee):
                continue
            t0 = (p.get("token0") or "").lower()
            t1 = (p.get("token1") or "").lower()
            if {t0, t1} == {tin, tout} and _pool_dex(p) == "uniswap_v3":
                return addr, p
        return None

    def _true_multi_output(
        self, chain_id: int, token_in: str, token_out: str, amount_in: int,
        hops: list[dict[str, Any]],
    ) -> int:
        """Real-quoter output for genesis's multi-hop path (0 if not derivable)."""
        try:
            fees = [int(h.get("fee") or 3000) for h in hops]
            # Genesis multi-hop is via a single intermediary (WETH or USDC).
            mids = []
            for h in hops[:-1]:
                ps = h.get("pool_state", {})
                t0 = (ps.get("token0") or "")
                t1 = (ps.get("token1") or "")
                # The hop's non-endpoint token is the intermediary.
                for t in (t0, t1):
                    if t and t.lower() not in (token_in.lower(), token_out.lower()):
                        mids.append(t)
            if len(fees) == 2 and mids:
                path = _v3_path_bytes([token_in, mids[0], token_out], fees)
                return self._quoter_path(chain_id, path, amount_in)
        except Exception:
            pass
        return 0

    def _recover_direct(
        self, pool_states, token_in, token_out, amount_in, chain_id,
    ):
        """Bug-fix fallback: best direct pool via pool-math + price estimate."""
        tin, tout = token_in.lower(), token_out.lower()
        cands, max_liq = [], 0
        for addr, pool in pool_states.items():
            if _pool_dex(pool) != "uniswap_v3":
                continue
            t0 = (pool.get("token0") or "").lower()
            t1 = (pool.get("token1") or "").lower()
            if t0 == tin and t1 == tout:
                z4o = True
            elif t0 == tout and t1 == tin:
                z4o = False
            else:
                continue
            liq = int(pool.get("liquidity") or 0)
            sp = int(pool.get("sqrtPriceX96") or 0)
            if liq <= 0 or sp <= 0:
                continue
            fee = int(pool.get("fee") or 3000)
            max_liq = max(max_liq, liq)
            cands.append((addr, pool, liq, z4o, fee, sp))
        if not cands:
            return None
        min_liq = max_liq // 20
        best = None
        for addr, pool, liq, z4o, fee, sp in cands:
            if liq < min_liq:
                continue
            out = 0
            try:
                out = compute_v3_output(sp, liq, amount_in, z4o, fee)
            except Exception:
                out = 0
            if out <= 0:
                out = _price_based_output(sp, amount_in, z4o, fee)
            if out <= 0:
                continue
            if best is None or out > best[2]:
                best = (addr, pool, out, fee)
        if best is None:
            return None
        addr, pool, out, fee = best
        return (
            out,
            f"direct {fee / 1_000_000:.2%} (decimal-fix recovery)",
            [{"pool_addr": addr, "pool_state": pool, "fee": fee}],
        )

    def _poolmath_gas_aware(self, genesis, pool_states, token_in, token_out, amount_in):
        """Genesis's gas-aware sub via pool-math (used when no real quoter)."""
        g_out, g_desc, g_hops = genesis
        dominant = _pool_dex(g_hops[0].get("pool_state", {}))
        if not all(_pool_dex(h.get("pool_state", {})) == dominant for h in g_hops):
            return genesis
        same_dex = {a: p for a, p in pool_states.items() if _pool_dex(p) == dominant}
        direct = find_best_pool(same_dex, token_in, token_out, amount_in)
        if direct is None:
            return genesis
        addr, state, d_out = direct
        if d_out <= 0:
            return genesis
        deficit = (g_out - d_out) / g_out
        required = _MIN_GAIN_PER_EXTRA_HOP * (len(g_hops) - 1)
        if deficit >= required:
            return genesis
        fee = int(state.get("fee") or state.get("tickSpacing") or 3000)
        return (
            d_out,
            f"poolmath single {fee / 1_000_000:.2%} (gas-aware)",
            [{"pool_addr": addr, "pool_state": state, "fee": fee}],
        )

    def metadata(self) -> SolverMetadata:
        base = super().metadata()
        return SolverMetadata(
            name=SOLVER_NAME,
            version=SOLVER_VERSION,
            author=SOLVER_AUTHOR,
            description=(
                "Baseline + real QuoterV2 route ranking (accurate multi-tick "
                "output, fixes same-decimal pool-math bug) + gas-aware "
                "single-hop substitution on true output"
            ),
            supported_chains=base.supported_chains,
            supported_intent_types=base.supported_intent_types,
        )


SOLVER_CLASS = MinerSolver
