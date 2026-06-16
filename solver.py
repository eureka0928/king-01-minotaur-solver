"""Minotaur Subnet 112 miner solver (king-01).

v5.0 — REAL-QUOTE ROUTING (beat genesis's delivered output) + v4.0 accurate quote.

The score is ``0.4*synthetic + 0.6*historical`` where each case's on-chain score
is ``scoreLinear(delivered, min)``. For HISTORICAL orders the min is the order's
ORIGINAL min (self-quote is skipped — they already carry quoted_output), so the
score there is pure ROUTING QUALITY: how much our plan DELIVERS. Re-pricing
(v3.1/v4.0) cannot move it — only delivering MORE output can. Historical is 60%
of the score, so routing is the dominant lever.

Genesis routes with single-tick ``compute_v3_output``, which ZEROES any pool swap
exceeding ~2% price impact (``delta_sqrt_price > sqrtPrice/100 -> return 0``) so
it finds NO route on large/illiquid orders and reverts to 0, and picks tiers by
no-slippage estimates. Measured on Base: genesis returns no route on 500+ WETH
while the real multi-tick QuoterV2 shows the same pool delivers ~868k USDC.

v5 overrides route SELECTION with the real QuoterV2: it scans the discovered
pools' tiers (and same-DEX 2-hops through USDC/WETH) by ACTUAL multi-tick output
and adopts a route only if it beats genesis's route. The plan is built by
genesis's OWN builders from the chosen hops; the quote is the accurate price.

WHERE THE WORK RUNS (the critical harness constraint):
  * ``quote()`` has a 5s harness timeout (Command.QUOTE). It must stay fast — it
    only RE-PRICES the already-chosen route (v4.0 logic, <=3 calls). It NEVER runs
    the route scan. Crucially, self-quote is only applied to SYNTHETIC scenarios
    (historical orders skip it), and synthetic orders are small enough that
    genesis routes them fine — so quote() pricing genesis's route is correct.
  * ``generate_plan()`` has a 30s budget. The expensive real-quote route SCAN runs
    ONLY here (gated by ``_king_allow_scan``). This is where the historical-order
    routing win is realised (delivered output is what the historical score grades).

Safety rails (v2.2 / xl-crash lessons, all verified):
  * NEVER REGRESS: adopt the v5 route only if genesis found NO route at all, OR
    v5's real output STRICTLY beats genesis's MEASURED real output by a margin. If
    genesis's route can't be measured (RPC flake -> 0), keep genesis — never
    switch on an unmeasured comparison.
  * EXECUTABLE ONLY: 2-hop candidates must be SAME-DEX (one router); direct always
    executes. Hops carry full pool_state, so genesis's builders accept them.
  * BOUNDED + DEADLINE: per-leg pool cap + a wall-clock budget under each harness
    timeout; a per-input decision cache (with TTL) avoids re-scanning. Never raises.
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
SOLVER_VERSION = os.environ.get("MINOTAUR_SOLVER_VERSION", "5.0.0")
SOLVER_AUTHOR = os.environ.get("MINOTAUR_SOLVER_AUTHOR", "king-01")

# Uniswap V3 QuoterV2 (uint24 fee) + Aerodrome Slipstream QuoterV2 (int24
# tickSpacing). Both are view eth_calls; never send a tx. Base only.
_UNI_QUOTER = {8453: "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"}
_AERO_QUOTER = {8453: "0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0"}
_SEL_UNI_SINGLE = "c6a5026a"   # quoteExactInputSingle((address,address,uint256,uint24,uint160))
_SEL_AERO_SINGLE = "9e7defe6"  # quoteExactInputSingle((address,address,uint256,int24,uint160))

# Conservative margin so realized output clears the returned estimate
# (outputScore = min(1, delivered/estimate) = 1.0) while staying just under
# delivery to minimise the app's CoW fee skim.
_REPRICE_SAFETY = float(os.environ.get("KING_REPRICE_SAFETY", "0.99"))    # 1%
_BLIND_SAFETY = float(os.environ.get("KING_BLIND_SAFETY", "0.90"))        # 10%

# Wall-clock budgets, each WELL UNDER its harness timeout (quote()=5s, plan=30s).
# quote() only re-prices the chosen route (<=3 calls). The route SCAN runs only in
# generate_plan(), where there is ample budget.
_QUOTE_BUDGET_S = float(os.environ.get("KING_QUOTE_BUDGET_S", "3.0"))     # quote() <5s cap
_ROUTE_BUDGET_S = float(os.environ.get("KING_ROUTE_BUDGET_S", "12.0"))    # generate_plan() <30s cap

# Route-decision cache TTL — invalidates a stale decision (its hops embed pool
# snapshots) so a long-lived process can't reuse a minutes-old route.
_DECISION_TTL_S = float(os.environ.get("KING_DECISION_TTL_S", "10.0"))

# Don't re-price absurdly long routes.
_MAX_REPRICE_HOPS = int(os.environ.get("KING_MAX_REPRICE_HOPS", "3"))

# Routing scan limits. v5 adopts a route only if it STRICTLY beats genesis's real
# delivered output by this margin (no churn on numerical ties). Pools-per-leg cap
# bounds the quoter fan-out.
_ROUTE_IMPROVE_MARGIN = float(os.environ.get("KING_ROUTE_IMPROVE", "1.003"))  # +0.3%
_MAX_LEG_POOLS = int(os.environ.get("KING_MAX_LEG_POOLS", "8"))

# Hub tokens for same-DEX 2-hop candidates (Base). USDC + WETH are the deep hubs.
_USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_WETH_BASE = "0x4200000000000000000000000000000000000006"
_ROUTE_INTERMEDIARIES = {8453: (_USDC_BASE, _WETH_BASE)}


class MinerSolver(BaselineSwapSolver):
    """Genesis baseline + real-quote route selection + QuoterV2-accurate quote.

    Routing is upgraded in generate_plan() (pick the route that REALLY delivers
    most); the plan is built by genesis's own builders from well-formed hops; the
    quote is the accurate price of the chosen route.
    """

    # ── quoter primitives ───────────────────────────────────────────────────
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

    @staticmethod
    def _hop_pool_dex(ps: dict[str, Any]) -> str:
        return (ps.get("dex") or "uniswap_v3").lower()

    def _quote_pool(self, chain_id, ps: dict[str, Any], tin, tout, amt) -> int:
        """Real multi-tick output for one pool, dispatched by venue. 0 on miss."""
        if amt <= 0:
            return 0
        dex = self._hop_pool_dex(ps)
        if "aerodrome" in dex:
            try:
                ts = int(ps.get("tickSpacing") or 0)
            except (TypeError, ValueError):
                return 0
            return self._aero_single(chain_id, tin, tout, amt, ts) if ts > 0 else 0
        try:
            fee = int(ps.get("fee") or 0)
        except (TypeError, ValueError):
            return 0
        return self._uni_single(chain_id, tin, tout, amt, fee) if fee > 0 else 0

    # ── accurate re-pricing of a chosen route (v4.0) ────────────────────────
    def _accurate_route_output(
        self, chain_id: int, hops: list[dict[str, Any]],
        token_in: str, token_out: str, amount_in: int, deadline: float,
    ) -> int:
        """Price a route leg-by-leg via the real quoter, chaining current->next.

        Returns the composed delivered output, or 0 to fall back.
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
                return 0
            out = self._quote_pool(chain_id, ps, current, nxt, amt)
            if out <= 0:
                return 0
            amt = out
            current = nxt
        if target and current != target:
            return 0
        return amt

    # ── real-quote route selection (v5) ─────────────────────────────────────
    def _pair_pools(self, pool_states, tin, tout):
        """Yield (addr, pool_state) for discovered pools matching {tin,tout}."""
        a, b = (tin or "").lower(), (tout or "").lower()
        n = 0
        for addr, p in (pool_states or {}).items():
            t0 = (p.get("token0") or "").lower()
            t1 = (p.get("token1") or "").lower()
            if {t0, t1} == {a, b}:
                yield addr, p
                n += 1
                if n >= _MAX_LEG_POOLS:
                    return

    def _best_real_leg(self, chain_id, pool_states, tin, tout, amt, deadline):
        """Best REAL single-hop output over discovered pools. (out, hop, dex) or None."""
        best_out, best_hop, best_dex = 0, None, None
        for addr, p in self._pair_pools(pool_states, tin, tout):
            if time.monotonic() > deadline:
                break
            out = self._quote_pool(chain_id, p, tin, tout, amt)
            if out > best_out:
                best_out = out
                best_dex = self._hop_pool_dex(p)
                best_hop = {
                    "pool_addr": addr, "pool_state": p, "fee": int(p.get("fee", 3000)),
                }
        return (best_out, best_hop, best_dex) if best_hop else None

    def _best_real_route(self, chain_id, pool_states, tin, tout, amt, deadline):
        """Route (direct + same-DEX 2-hop) with the max REAL delivered output.

        Returns (out, hops) or (0, None). 2-hop legs must share a DEX (one router).
        """
        best_out, best_hops = 0, None
        direct = self._best_real_leg(chain_id, pool_states, tin, tout, amt, deadline)
        if direct:
            best_out, best_hops = direct[0], [direct[1]]
        for mid in _ROUTE_INTERMEDIARIES.get(int(chain_id), ()):  # type: ignore[arg-type]
            if time.monotonic() > deadline:
                break
            ml = (mid or "").lower()
            if ml in ((tin or "").lower(), (tout or "").lower()):
                continue
            leg1 = self._best_real_leg(chain_id, pool_states, tin, mid, amt, deadline)
            if not leg1 or leg1[0] <= 0:
                continue
            leg2 = self._best_real_leg(chain_id, pool_states, mid, tout, leg1[0], deadline)
            if not leg2 or leg2[0] <= 0:
                continue
            if leg1[2] != leg2[2]:   # same-DEX only (executable in one router)
                continue
            if leg2[0] > best_out:
                best_out, best_hops = leg2[0], [leg1[1], leg2[1]]
        return best_out, best_hops

    # ── plan generation: enable the route scan here (30s budget) ────────────
    def generate_plan(self, intent, state, snapshot=None):
        """Genesis plan generation, with the v5 route scan enabled.

        The scan is heavy (quoter fan-out), so it runs ONLY in this 30s-budget
        path — never in the 5s quote() path. generate_plan routes via our
        _find_best_executable_route override, which (with the scan flag on) picks
        the real-quote-best route and builds the plan from it via genesis's
        builders.
        """
        self._king_allow_scan = True
        try:
            return super().generate_plan(intent, state, snapshot)
        finally:
            self._king_allow_scan = False

    def _find_best_executable_route(
        self,
        pool_states: dict[str, dict[str, Any]],
        token_in: str, token_out: str, amount_in: int, chain_id: int,
    ):
        """Pick the route that REALLY delivers most (scan in generate_plan only).

        In the quote() path (scan flag off) this is genesis's route, unchanged and
        fast — we only stash it for re-pricing. In the generate_plan() path (scan
        flag on) it runs the real-quote scan and adopts a strictly-better route,
        never regressing.
        """
        genesis = super()._find_best_executable_route(
            pool_states, token_in, token_out, amount_in, chain_id,
        )

        try:
            amt = int(amount_in)
        except (TypeError, ValueError):
            amt = 0
        try:
            key = ((token_in or "").lower(), (token_out or "").lower(), amt, int(chain_id))
        except (TypeError, ValueError):
            key = None

        now = time.monotonic()
        allow_scan = bool(getattr(self, "_king_allow_scan", False))

        # Reuse a fresh decision for the same key — but if it was made WITHOUT a
        # scan and we are now allowed to scan (generate_plan), re-evaluate.
        cache = getattr(self, "_king_route_decision", None)
        if (
            cache is not None and key is not None and cache.get("key") == key
            and (now - cache.get("t", 0.0)) < _DECISION_TTL_S
            and (cache.get("scanned") or not allow_scan)
        ):
            chosen = cache.get("route") if cache.get("route") is not None else genesis
            self._king_last_route = {"key": key, "hops": chosen[2] if chosen else None}
            return chosen

        chosen = genesis
        if allow_scan:
            try:
                if pool_states and amt > 0 and token_in and token_out:
                    deadline = now + _ROUTE_BUDGET_S
                    v_out, v_hops = self._best_real_route(
                        chain_id, pool_states, token_in, token_out, amt, deadline,
                    )
                    if v_hops and v_out > 0:
                        if genesis is None:
                            # genesis found NO executable route -> any deliverable
                            # v5 route is strictly better.
                            chosen = (v_out, "king v5 real-route", v_hops)
                        else:
                            g_real = self._accurate_route_output(
                                chain_id, genesis[2], token_in, token_out, amt, deadline,
                            ) or 0
                            # Switch ONLY on a measured, strict improvement. If
                            # g_real is 0 (couldn't price genesis's route — RPC
                            # flake or genuine 0), keep genesis: never adopt on an
                            # unmeasured comparison.
                            if g_real > 0 and v_out > g_real * _ROUTE_IMPROVE_MARGIN:
                                chosen = (v_out, "king v5 real-route", v_hops)
                                logger.info(
                                    "king-01 v5 route: %s->%s amt=%d  genesis_real=%d -> v5=%d (hops=%d)",
                                    token_in[:8], token_out[:8], amt, g_real, v_out, len(v_hops),
                                )
            except Exception:
                chosen = genesis  # routing override must never raise

        if key is not None:
            self._king_route_decision = {
                "key": key, "route": chosen, "t": now, "scanned": allow_scan,
            }
        self._king_last_route = {"key": key, "hops": chosen[2] if chosen else None}
        return chosen

    # ── quote: fast accurate price of the chosen route (v4.0, <5s) ──────────
    def quote(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot=None,
    ) -> QuoteResult:
        q = super().quote(intent, state, snapshot)  # routes via our override (no scan)

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

        # Cross-chain orders route via genesis's _quote_cross_chain (NOT our
        # single-chain router), so there is no single-chain route to re-price and
        # the decision cache would be stale — return genesis's quote unchanged.
        in_chain = swap.get("_input_chain", state.chain_id)
        out_chain = swap.get("_output_chain", state.chain_id)
        try:
            if in_chain and out_chain and int(in_chain) != int(out_chain):
                return q
        except (TypeError, ValueError):
            pass

        meta = q.metadata or {}
        hops_n = int(meta.get("hops") or 0)

        accurate = 0
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
                accurate = 0

        if accurate <= 0:
            accurate = int(genesis_est * _BLIND_SAFETY)

        # accurate = real(chosen route) * 0.99 <= genesis_est both when we kept
        # genesis's route (single-tick over-estimate) and when generate_plan later
        # re-routes (genesis_est here reflects genesis's route; the plan may
        # deliver MORE, which only loosens the min — never a revert).
        new_est = min(genesis_est, accurate) if accurate > 0 else genesis_est
        if new_est <= 0 or new_est == genesis_est:
            return q

        logger.info(
            "king-01 v5 quote: genesis_est=%d -> est=%d (hops=%d, ratio=%.3f)",
            genesis_est, new_est, hops_n, (new_est / genesis_est) if genesis_est else 0,
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
                "Genesis baseline + real-quote route selection in generate_plan "
                "(beats genesis's single-tick router on large/illiquid orders it "
                "zeroes) + QuoterV2-accurate conservative quote; plans built by "
                "genesis's own builders from well-formed hops"
            ),
            supported_chains=base.supported_chains,
            supported_intent_types=base.supported_intent_types,
        )


SOLVER_CLASS = MinerSolver
