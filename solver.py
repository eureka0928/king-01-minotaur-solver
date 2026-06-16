"""Minotaur Subnet 112 miner solver (king-01).

v6.0 — ROBUST REAL-QUOTE ROUTING. v5's real-quote route selection, made
*structurally incapable* of the timeout->worker-kill that sank v5.0.2.

What killed v5.0.2 (scored 0.31): genesis discovers pools via LIVE, UNBOUNDED
RPC inside ``generate_plan()``/``quote()`` — ``_get_web3`` has no timeout and
``_ensure_pools_for_route`` queries the factory across many fee tiers + every
intermediary + Aerodrome. On a heavy multi-hop pair (cbBTC: no direct pool) that
discovery alone sits near the 30s worker cap; v5's added scan pushed it OVER, so
the harness KILLED the worker — and the very next case, the single historical
order (60% of the score), found a dead process and crashed -> historical_avg=0.
0.4*0.7695 + 0.6*0.0 = 0.31. One timeout, via the cascade, cost ~0.55 of score.

v6 robustness layers — each guarantees we return well under the harness cap no
matter how slow or hung the RPC is, so the worker is NEVER killed and the
cascade is structurally impossible:

  1. HARD WATCHDOG. ``generate_plan()``/``quote()`` run their real work in a
     daemon thread joined with a hard deadline (plan 22s < 30s; quote 4s < 5s).
     If it doesn't finish in time we return a SNAPSHOT-ONLY fallback (no RPC ->
     instant). A hung RPC can stall the work thread forever; the watchdog still
     returns on time.
  2. BOUNDED, THREAD-LOCAL DISCOVERY. ``_get_web3`` is overridden to add a hard
     per-call timeout (so genesis's own discovery calls can't hang) and to be
     THREAD-LOCAL (so a timed-out, abandoned work thread never shares a socket
     with the next case's thread).
  3. THREAD-LOCAL GATES. the scan gate / scan deadline / snapshot-only flag live
     in a ``threading.local``, so the main-thread fallback never races the
     abandoned work thread on shared mutable flags.

v6.1 hardening (from adversarial review — each was a confirmed worker-kill or
regression the v6.0 watchdog alone did NOT close):
  * RETRIES DISABLED. web3 7.x silently retries 5x on the eth_call/eth_gasPrice
    allowlist, multiplying our per-call timeout ~5x (3s -> ~17s). ``_make_web3``
    sets retries=1 so the per-call timeout is a TRUE ceiling on every provider.
  * GAS PRICE GATED. genesis's ``_get_gas_price_wei`` issues an UNGATED live
    eth_gasPrice that the snapshot-only fallback otherwise hit on the MAIN thread
    AFTER the join — ~21s > the 5s quote cap = worker kill. It is now overridden
    to return the static chain fallback under ``snapshot_only`` (gas only feeds
    fee metadata, never output/routing), making the fallback truly RPC-free.
  * DEADLINES near the caps (plan 27s / quote 4.7s). The fallback is RPC-free and
    near-instant, so an over-conservative 22s/4s only made the watchdog fire
    BELOW the window genesis itself gets — regressing to a blind snapshot plan on
    heavy pairs genesis would have finished under the cap.
  * PRIVATE POOL DICTS. ``_get_pool_states`` returns a (retry-safe) copy so an
    abandoned work thread inserting into ``self._pool_cache`` can't trip
    'dictionary changed size during iteration' in the next case.
  * NON-REENTRANT WATCHDOG. an ``in_watchdog`` guard makes the substrate-EVM-leg
    recursion run the genesis body inline instead of stacking a second 22s+22s
    watchdog (which could double main-thread time past the 30s cap).

Everything else is v5: real-QuoterV2 route SELECTION in ``generate_plan()`` (beats
genesis's single-tick router on the large/illiquid orders it zeroes — genesis's
``compute_v3_output`` ZEROES any pool swap over ~2% price impact, so it finds NO
route on 500+ WETH while the real multi-tick QuoterV2 shows ~868k USDC), an
accurate conservative ``quote()``, a strict NEVER-REGRESS guard (adopt a v5 route
only if genesis found none, or v5's MEASURED real output strictly beats genesis's
by a margin), and plans built by genesis's OWN builders from well-formed hops.

Scoring model: ``0.4*synthetic_avg + 0.6*historical_avg``; each case scores
``scoreLinear(delivered, min)`` (0 if delivered<min, 10000 capped if
delivered>=2*min). HISTORICAL orders keep their ORIGINAL min (self-quote skipped
— they carry quoted_output), so their score is pure ROUTING/delivered quality:
re-pricing can't move it, only delivering MORE output can. Historical is 60% of
the score and a single crash there zeroes the whole 60% — so ROBUSTNESS dominates
routing gains, which is exactly what v6 buys.

WHERE THE WORK RUNS (the harness constraint):
  * ``quote()`` (5s cap): only RE-PRICES the already-chosen route (<=3 calls).
    NEVER runs the route scan.
  * ``generate_plan()`` (30s cap): the real-quote route SCAN runs ONLY here,
    gated by a thread-local flag, inside the watchdog thread.
"""

from __future__ import annotations

import logging
import os
import threading
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

# Eager-warm the modules the WATCHDOG FALLBACK imports lazily. Genesis's quote
# imports `minotaur_subnet.blockchain.tokens` (line ~1941) and `pool_math` inside
# the function; on the very FIRST quote, if the work thread hangs in discovery
# (never reaching those imports) the main-thread fallback would pay the cold
# ~210ms import cost AFTER already spending the 4.6s join — shrinking the 5s
# margin toward a kill on a slow container. Paying it once here (inside the 60s
# initialize budget, off the hot path) closes that window. Best-effort: a missing
# module must not break solver load.
try:  # noqa: SIM105
    import minotaur_subnet.blockchain.tokens as _warm_tokens  # noqa: F401
    import strategies.dex_aggregator.pool_math as _warm_pool_math  # noqa: F401
except Exception:
    pass

logger = logging.getLogger(__name__)


SOLVER_NAME = os.environ.get("MINOTAUR_SOLVER_NAME", "king-01-solver")
SOLVER_VERSION = os.environ.get("MINOTAUR_SOLVER_VERSION", "6.2.1")
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

# Hard PER-CALL timeout for our quoter eth_calls (seconds). A hung RPC otherwise
# inherits web3's 30s default and blows the worker budget despite the wall-clock
# deadline (which is only checked BETWEEN calls).
_RPC_CALL_TIMEOUT_S = float(os.environ.get("KING_RPC_CALL_TIMEOUT_S", "2.5"))

# Hard PER-CALL timeout for genesis's OWN pool-discovery calls. v5 left these on
# web3's unbounded default — the direct cause of the cbBTC_to_WETH 30s kill.
_DISCOVERY_CALL_TIMEOUT_S = float(os.environ.get("KING_DISCOVERY_CALL_TIMEOUT_S", "3.0"))

# WATCHDOG hard deadlines. These are the wall-clock ceilings on the whole call;
# if the work thread isn't done by then we return a snapshot-only fallback. Set
# safely under the harness caps (quote 5s, generate_plan 30s) with room for the
# thread join + the (RPC-free) fallback.
# Set close to the harness caps (5s / 30s): the fallback is now RPC-free and
# near-instant, so we only need a small margin for the thread join + fallback.
# Earlier (4.0 / 22.0) the watchdog fired BELOW the window genesis itself gets,
# regressing to a blind snapshot plan on heavy pairs (cbBTC) that genesis would
# have completed under the cap. Give the work thread (nearly) the full window.
_HARD_QUOTE_DEADLINE_S = float(os.environ.get("KING_HARD_QUOTE_DEADLINE_S", "4.6"))
_HARD_PLAN_DEADLINE_S = float(os.environ.get("KING_HARD_PLAN_DEADLINE_S", "27.0"))
# Hard clamps: never let an env override push a deadline so close to the harness
# cap (5s / 30s) that the thread join + RPC-free fallback could cross it.
_HARD_QUOTE_DEADLINE_S = min(_HARD_QUOTE_DEADLINE_S, 4.7)
_HARD_PLAN_DEADLINE_S = min(_HARD_PLAN_DEADLINE_S, 28.5)

# Budgets used WITHIN the work thread. The scan's internal deadline is measured
# from the work thread's entry and kept well under the plan watchdog so the scan
# yields before the watchdog ever fires.
_SCAN_PLAN_BUDGET_S = float(os.environ.get("KING_SCAN_PLAN_BUDGET_S", "16.0"))
_ROUTE_BUDGET_S = float(os.environ.get("KING_ROUTE_BUDGET_S", "12.0"))      # scan slice
_QUOTE_BUDGET_S = float(os.environ.get("KING_QUOTE_BUDGET_S", "2.5"))       # repricing slice

# Route-decision cache TTL — its hops embed pool snapshots, so a stale entry must
# not be reused minutes later.
_DECISION_TTL_S = float(os.environ.get("KING_DECISION_TTL_S", "10.0"))

# Don't re-price absurdly long routes.
_MAX_REPRICE_HOPS = int(os.environ.get("KING_MAX_REPRICE_HOPS", "3"))

# Adopt a v5 route only if it STRICTLY beats genesis's real output by this margin.
# 2-hops carry extra slippage/gas the per-leg single-quoter doesn't capture, so
# they need a wider margin.
_ROUTE_IMPROVE_MARGIN = float(os.environ.get("KING_ROUTE_IMPROVE", "1.003"))        # +0.3% direct
_ROUTE_IMPROVE_MARGIN_2HOP = float(os.environ.get("KING_ROUTE_IMPROVE_2HOP", "1.01"))  # +1% 2-hop
_MAX_LEG_POOLS = int(os.environ.get("KING_MAX_LEG_POOLS", "8"))

# Fallback hub tokens for same-DEX 2-hop (Base) when the registry lookup fails.
_USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_WETH_BASE = "0x4200000000000000000000000000000000000006"
_ROUTE_INTERMEDIARIES = {8453: (_USDC_BASE, _WETH_BASE)}


class MinerSolver(BaselineSwapSolver):
    """Genesis baseline + real-quote route selection + watchdog robustness."""

    # ── thread-local gates (scan flag / deadline / snapshot-only) ────────────
    def _tls(self) -> threading.local:
        tls = getattr(self, "_king_tls", None)
        if tls is None:
            tls = self._king_tls = threading.local()
        return tls

    # ── hard watchdog: run work in a daemon thread, fall back if it overruns ─
    def _run_with_watchdog(self, work, deadline_s: float, fallback):
        """Return ``work()`` if it finishes within ``deadline_s``; else (or if it
        raised) return ``fallback()``. The work thread is a daemon, so an
        abandoned (hung) one never blocks process exit. We NEVER block past
        ``deadline_s`` — that is the whole point: the harness must never see a
        timeout and kill the worker.
        """
        box: dict[str, Any] = {}

        def _runner():
            try:
                box["v"] = work()
            except BaseException as exc:  # noqa: BLE001 — capture everything
                box["e"] = exc

        t = threading.Thread(target=_runner, name="king-watchdog", daemon=True)
        t.start()
        t.join(deadline_s)

        if not t.is_alive() and "v" in box:
            return box["v"]

        # Timed out (still alive) or finished by raising: take the fast,
        # RPC-free fallback. If even that fails, re-raise the original error
        # (a clean error keeps the worker alive — only a TIMEOUT kills it, and
        # the watchdog has already prevented that).
        try:
            return fallback()
        except Exception:
            if "e" in box:
                raise box["e"]
            raise

    # ── web3 builder: hard per-call timeout AND retries DISABLED ─────────────
    @staticmethod
    def _make_web3(rpc_url: str, timeout: float):
        """Build a Web3 whose per-call timeout is a TRUE wall-clock ceiling.

        web3 7.x defaults the HTTPProvider to retries=5 on the
        eth_call/eth_gasPrice/eth_getStorageAt allowlist, silently multiplying
        the requests ``timeout`` ~5x (a 3s call becomes ~17s on a hung/throttling
        RPC). That defeats the per-call bound the watchdog budgets around and was
        a confirmed worker-kill path. retries=1 (one attempt) restores the
        per-call timeout as a real ceiling.
        """
        from web3 import Web3
        prov = Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": timeout})
        try:
            from web3.providers.rpc.utils import ExceptionRetryConfiguration
            import requests as _rq
            prov.exception_retry_configuration = ExceptionRetryConfiguration(
                errors=(
                    _rq.exceptions.ConnectionError,
                    _rq.exceptions.HTTPError,
                    _rq.exceptions.Timeout,
                ),
                retries=1,
                backoff_factor=0.0,
            )
        except Exception:
            try:
                prov.exception_retry_configuration.retries = 1
            except Exception:
                pass
        return Web3(prov)

    # ── bounded, thread-local discovery web3 (overrides genesis's unbounded) ─
    def _get_web3(self, chain_id: int) -> Any:
        """Genesis's discovery web3, but with a HARD per-call timeout and kept
        THREAD-LOCAL. Bounding the per-call timeout stops a single hung discovery
        call from eating the whole budget (v5.0.2's failure); the thread-local
        cache stops an abandoned work thread from sharing a requests session with
        the next case's thread.
        """
        tls = self._tls()
        cache = getattr(tls, "disc_web3", None)
        if cache is None:
            cache = tls.disc_web3 = {}
        if chain_id in cache:
            return cache[chain_id]
        rpc_url = self._rpc_urls.get(chain_id)
        if not rpc_url:
            cache[chain_id] = None
            return None
        try:
            w3 = self._make_web3(rpc_url, _DISCOVERY_CALL_TIMEOUT_S)
            cache[chain_id] = w3
            return w3
        except Exception:
            cache[chain_id] = None
            return None

    def _get_gas_price_wei(self, chain_id: int) -> int:
        """In the snapshot-only fallback (post-watchdog, MAIN thread) never do
        live RPC. Genesis's _get_gas_price_wei issues a live eth_gasPrice that is
        otherwise UNGATED — on a hung RPC it runs AFTER the watchdog join has
        already spent most of the budget, blowing the cap and killing the worker
        (confirmed critical). Gas price only feeds fee metadata (not
        estimated_output or routing), so the static chain fallback costs nothing
        on score.
        """
        if getattr(self._tls(), "snapshot_only", False):
            try:
                from strategies.dex_aggregator.baseline_solver import (
                    _FALLBACK_GAS_PRICE_WEI, _GENERIC_FALLBACK_GAS_PRICE_WEI,
                )
                return _FALLBACK_GAS_PRICE_WEI.get(
                    int(chain_id), _GENERIC_FALLBACK_GAS_PRICE_WEI,
                )
            except Exception:
                return 1_000_000_000  # 1 gwei generic
        return super()._get_gas_price_wei(chain_id)

    # ── snapshot-only path (used by the watchdog fallback — no RPC) ──────────
    @staticmethod
    def _safe_copy(d):
        """Copy a pool-states dict that an ABANDONED watchdog work thread may be
        concurrently mutating. dict(d) can raise 'dictionary changed size during
        iteration'; inserts are sparse (after multi-second RPC batches), so a few
        retries reliably capture a stable snapshot."""
        if not d:
            return {}
        for _ in range(6):
            try:
                return dict(d)
            except RuntimeError:
                continue
        try:
            return {k: v for k, v in list(d.items())}
        except RuntimeError:
            return {}

    def _get_pool_states(self, chain_id, snapshot):
        if getattr(self._tls(), "snapshot_only", False):
            if snapshot is not None and snapshot.pool_states:
                return dict(snapshot.pool_states)
            return {}
        # Private copy: genesis returns self._pool_cache[chain_id] BY REFERENCE,
        # which an abandoned work thread from a prior case may still be inserting
        # into. Work on our own dict so iteration here (and genesis's) can't race
        # that mutation -> no "dictionary changed size during iteration".
        return self._safe_copy(super()._get_pool_states(chain_id, snapshot))

    def _discover_pools(self, chain_id):
        """Genesis's _discover_pools iterates self._pair_discovery_cache
        (`stale = [k for k in self._pair_discovery_cache ...]`) which an ABANDONED
        watchdog work thread may still be inserting into -> 'dictionary changed
        size during iteration'. That fires INSIDE this call (before any value is
        returned), so _safe_copy on the return value can't catch it. Inserts are
        sparse (one per multi-second RPC batch); retry a few times, then fall back
        to the last good cache."""
        for _ in range(6):
            try:
                return super()._discover_pools(chain_id)
            except RuntimeError:
                continue
        try:
            return getattr(self, "_pool_cache", {}).get(chain_id, {})
        except Exception:
            return {}

    def _ensure_pools_for_route(self, chain_id, pool_states, token_in, token_out):
        if getattr(self._tls(), "snapshot_only", False):
            return pool_states  # no live discovery in the fallback
        return super()._ensure_pools_for_route(chain_id, pool_states, token_in, token_out)

    def _generate_yield_plan(self, intent, state, snapshot=None):
        """The yield/rebalance path delegates to BaselineYieldStrategy, which
        queries Aave/Compound rates via raw urllib (timeout=10s/call) — bypassing
        our bounded web3 entirely. In the snapshot-only FALLBACK (post-watchdog,
        MAIN thread) it must do ZERO network I/O: otherwise it re-runs the ~30s
        urllib sweep on top of the 27s join and blows the 30s cap (worker kill +
        cascade). The yield strategy no-ops both rate queries when its rpc_url is
        falsy (its only source is os.environ['ANVIL_RPC_URL']) and returns a
        deterministic Aave-default plan — so clearing it makes the fallback
        instant. The work-thread path keeps live rate optimisation; if it overruns
        the watchdog, it is abandoned and this RPC-free fallback bounds the total.
        """
        if not getattr(self._tls(), "snapshot_only", False):
            return super()._generate_yield_plan(intent, state, snapshot)
        prev = os.environ.get("ANVIL_RPC_URL")
        try:
            if prev:
                os.environ["ANVIL_RPC_URL"] = ""
            return super()._generate_yield_plan(intent, state, snapshot)
        finally:
            if prev:
                os.environ["ANVIL_RPC_URL"] = prev

    # ── quoter primitives (timeout-bounded, thread-local) ───────────────────
    def _quoter_web3(self, chain_id: int):
        """A dedicated web3 for our quoter calls, with a HARD per-call timeout,
        thread-local so concurrent (abandoned + live) threads never share it."""
        try:
            cid = int(chain_id)
        except (TypeError, ValueError):
            return None
        tls = self._tls()
        cache = getattr(tls, "quoter_web3", None)
        if cache is None:
            cache = tls.quoter_web3 = {}
        if cid in cache:
            return cache[cid]
        rpc_url = self._rpc_urls.get(cid)
        if not rpc_url:
            cache[cid] = None
            return None
        try:
            w3 = self._make_web3(rpc_url, _RPC_CALL_TIMEOUT_S)
            cache[cid] = w3
            return w3
        except Exception:
            cache[cid] = None
            return None

    def _qcall(self, chain_id: int, to_addr: str, data_hex: str) -> int:
        """One view eth_call (timeout-bounded). First 32 bytes as int, 0 on any failure."""
        if not to_addr:
            return 0
        try:
            w3 = self._quoter_web3(int(chain_id))
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
    def _route_intermediaries(self, chain_id: int):
        """Hub tokens for 2-hop, preferring genesis's registry set (so we scan the
        same hubs genesis discovered pools for); falls back to our constants."""
        try:
            hubs = self._intermediaries_for_chain(int(chain_id))
            if hubs:
                return tuple(hubs)
        except Exception:
            pass
        return _ROUTE_INTERMEDIARIES.get(int(chain_id), ())

    def _pair_pools(self, pool_states, tin, tout):
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
                # NB: "fee" here is dead for Aerodrome hops — the Aero plan builders
                # read tickSpacing from pool_state, not hop["fee"]. Don't start
                # trusting hop["fee"] for Aero in future edits.
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
        for mid in self._route_intermediaries(chain_id):
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

    # ── plan generation: watchdog + the route scan (30s budget) ─────────────
    def generate_plan(self, intent, state, snapshot=None):
        """Genesis plan generation with the v5 route scan, wrapped in a hard
        watchdog. The scan-enabled work runs in a daemon thread; if it overruns
        the watchdog we return a snapshot-only plan (no RPC) rather than let the
        harness time out and kill the worker.
        """
        # Reentrancy guard: genesis's substrate-to-EVM path recurses into
        # self.generate_plan for the EVM leg. Never stack a second watchdog (that
        # would let the main-thread time double past the 30s cap); run the genesis
        # body inline within the already-running outer budget (and, in the
        # fallback, under the inherited snapshot_only -> RPC-free).
        if getattr(self._tls(), "in_watchdog", False):
            return BaselineSwapSolver.generate_plan(self, intent, state, snapshot)

        def _work():
            tls = self._tls()
            tls.in_watchdog = True
            tls.allow_scan = True
            tls.snapshot_only = False
            tls.deadline = time.monotonic() + _SCAN_PLAN_BUDGET_S
            try:
                return BaselineSwapSolver.generate_plan(self, intent, state, snapshot)
            finally:
                tls.in_watchdog = False
                tls.allow_scan = False
                tls.deadline = None

        def _fallback():
            tls = self._tls()  # main thread's TLS — independent of the work thread
            prev = getattr(tls, "snapshot_only", False)
            tls.in_watchdog = True
            tls.allow_scan = False
            tls.snapshot_only = True
            try:
                return BaselineSwapSolver.generate_plan(self, intent, state, snapshot)
            finally:
                tls.in_watchdog = False
                tls.snapshot_only = prev

        return self._run_with_watchdog(_work, _HARD_PLAN_DEADLINE_S, _fallback)

    def _find_best_executable_route(
        self,
        pool_states: dict[str, dict[str, Any]],
        token_in: str, token_out: str, amount_in: int, chain_id: int,
    ):
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
        tls = self._tls()
        allow_scan = bool(getattr(tls, "allow_scan", False))

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
                    # Bound the scan by BOTH a fresh slice budget and the absolute
                    # scan deadline (covers prior genesis discovery time).
                    plan_dl = getattr(tls, "deadline", None)
                    deadline = now + _ROUTE_BUDGET_S
                    if plan_dl is not None:
                        deadline = min(deadline, plan_dl)
                    v_out, v_hops = self._best_real_route(
                        chain_id, pool_states, token_in, token_out, amt, deadline,
                    )
                    if v_hops and v_out > 0:
                        if genesis is None:
                            # genesis found NO executable route -> any deliverable
                            # v5 route is strictly better than reverting to 0.
                            chosen = (v_out, "king v5 real-route", v_hops)
                        else:
                            g_real = self._accurate_route_output(
                                chain_id, genesis[2], token_in, token_out, amt, deadline,
                            ) or 0
                            margin = (
                                _ROUTE_IMPROVE_MARGIN_2HOP if len(v_hops) > 1
                                else _ROUTE_IMPROVE_MARGIN
                            )
                            # Switch ONLY on a measured, strict improvement. g_real
                            # == 0 (unmeasurable -> RPC flake or genuine 0) keeps
                            # genesis: never switch on an unmeasured comparison.
                            if g_real > 0 and v_out > g_real * margin:
                                chosen = (v_out, "king v5 real-route", v_hops)
                                logger.info(
                                    "king-01 v6 route: %s->%s amt=%d  genesis_real=%d -> v5=%d (hops=%d)",
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
    def _is_cross_chain(self, state, swap) -> bool:
        """Mirror genesis's cross-chain detection (CAIP chain ids OR dest_chain_id)."""
        try:
            in_chain = swap.get("_input_chain", state.chain_id)
            out_chain = swap.get("_output_chain", state.chain_id)
            if in_chain and out_chain and int(in_chain) != int(out_chain):
                return True
        except (TypeError, ValueError):
            pass
        try:
            from strategies.dex_aggregator.baseline_solver import _cross_chain_compat_params
            dest = _cross_chain_compat_params(state).get("dest_chain_id")
            # Only cross-chain when dest differs from the source chain — a
            # self-referential dest == chain_id is a normal same-chain order
            # (genesis treats it as such), so don't skip our re-pricing.
            if dest and int(dest) != int(state.chain_id):
                return True
        except Exception:
            pass
        return False

    def quote(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot=None,
    ) -> QuoteResult:
        """Accurate conservative quote, wrapped in a hard watchdog. The real work
        (genesis quote + our re-pricing, which still does live discovery) runs in
        a daemon thread; on overrun we return a snapshot-only genesis quote rather
        than risk a 5s timeout that would kill the worker.
        """
        if getattr(self._tls(), "in_watchdog", False):
            return self._quote_impl(intent, state, snapshot)

        def _work():
            tls = self._tls()
            tls.in_watchdog = True
            try:
                return self._quote_impl(intent, state, snapshot)
            finally:
                tls.in_watchdog = False

        def _fallback():
            tls = self._tls()
            prev = getattr(tls, "snapshot_only", False)
            tls.in_watchdog = True
            tls.snapshot_only = True
            try:
                q = BaselineSwapSolver.quote(self, intent, state, snapshot)
                # The genesis snapshot estimate is priced from stale synthetic
                # pools (hardcoded prices). For SYNTHETIC orders the harness sets
                # min = estimate*0.5, so an estimate above the live-delivered
                # output can push min over delivery -> revert -> 0. Apply the same
                # conservative reduction the main path uses so the fallback's min
                # can never exceed what the live path would have set.
                try:
                    est = int(q.estimated_output or 0)
                    if est > 0:
                        return QuoteResult(
                            estimated_output=str(int(est * _BLIND_SAFETY)),
                            computed_params=dict(q.computed_params or {}),
                            route_summary=q.route_summary,
                            gas_estimate=q.gas_estimate,
                            metadata={**(q.metadata or {}), "king_fallback": True},
                            platform_fee_wei=q.platform_fee_wei,
                            platform_fee_token=q.platform_fee_token,
                            platform_fee_symbol=q.platform_fee_symbol,
                        )
                except Exception:
                    pass
                return q
            finally:
                tls.in_watchdog = False
                tls.snapshot_only = prev

        return self._run_with_watchdog(_work, _HARD_QUOTE_DEADLINE_S, _fallback)

    def _quote_impl(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot,
    ) -> QuoteResult:
        q = BaselineSwapSolver.quote(self, intent, state, snapshot)  # via our override (no scan)

        # Cross-chain orders route through genesis's bridge path (estimated_output
        # = bridged, computed_params carry the bridge min). Do NOT re-price/slash
        # them — that would emit min_output > estimate. Return genesis's quote.
        if (q.metadata or {}).get("cross_chain"):
            return q

        try:
            genesis_est = int(q.estimated_output or 0)
        except (TypeError, ValueError):
            return q
        if genesis_est <= 0:
            return q

        swap = self._normalized_swap_params(intent, state)
        if self._is_cross_chain(state, swap):
            return q

        token_in = swap.get("input_token", "")
        token_out = swap.get("output_token", "")
        try:
            amount_in = int(swap.get("input_amount", 0) or 0)
        except (TypeError, ValueError):
            amount_in = 0

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

        new_est = min(genesis_est, accurate) if accurate > 0 else genesis_est
        if new_est <= 0 or new_est == genesis_est:
            return q

        logger.info(
            "king-01 v6 quote: genesis_est=%d -> est=%d (hops=%d, ratio=%.3f)",
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
                "zeroes) + QuoterV2-accurate conservative quote; hard watchdog + "
                "bounded thread-local discovery make timeouts/worker-kills "
                "structurally impossible; plans built by genesis's own builders"
            ),
            supported_chains=base.supported_chains,
            supported_intent_types=base.supported_intent_types,
        )


SOLVER_CLASS = MinerSolver
