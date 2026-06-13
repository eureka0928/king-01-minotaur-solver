# Bug report: the DexAggregator benchmark is currently uncontestable

**Subnet:** 112 (Minotaur) · **App:** `app_da6c96b84c60` (DexAggregatorApp, Base 8453)
**Severity:** High — no challenger can dethrone the champion regardless of solver quality
**Reporter:** miner `king-01` (hotkey `5CM7UrTtmsPG8W74BwNvUFwg3T1k31dro933roWGDwKZjUap`)
**Date:** 2026-06-13

## TL;DR

The synthetic `WETH_to_USDC*` benchmark scenarios carry **hard-coded
`min_output_amount` values that imply an ETH price of $1,800–$2,000**. ETH on
Base is currently **~$1,663**. Because the benchmark feeds those literal mins
straight into the swap as `amountOutMinimum` (no re-quote), **every solver's
WETH→USDC swap reverts** with `CallFailed(1, "Too little received")` and scores
0. The reverse `USDC_to_WETH*` cases pass for the opposite reason (their mins now
imply ETH ≤ $4,000, trivially satisfied).

Net effect: the live genesis baseline scores **~0.287** on the current 22-case
pack, the theoretical ceiling for *any* solver is **~0.5**, but the adoption gate
requires beating the champion's **stored 0.62** (`score_to_beat = 0.6231`). That
0.62 was recorded against an older pack when these cases were winnable. **No
submission can clear 0.6231 on today's pack**, so the contest is mathematically
uncontestable until the mins are refreshed or the champion is re-benchmarked.

## Evidence

### 1. The scenario mins imply a stale ETH price

Decoded from the live scorer manifest (`GET /v1/apps/app_da6c96b84c60`):

| Scenario | input → min output | implied ETH |
|---|---|---|
| `WETH_to_USDC` | 1 WETH → ≥ 1,800 USDC | **≥ $1,800** |
| `WETH_to_USDC_tiny` | 0.0005 WETH → ≥ 1.00 USDC | **≥ $2,000** |
| `WETH_to_USDC_medium` | 0.1 WETH → ≥ 200 USDC | **≥ $2,000** |
| `WETH_to_USDC_large` | 1 WETH → ≥ 2,000 USDC | **≥ $2,000** |
| `WETH_to_USDC_xl` | 5 WETH → ≥ 10,000 USDC | **≥ $2,000** |
| `USDC_to_WETH_tiny` | 2 USDC → ≥ 0.0005 WETH | ≤ $4,000 (passes) |

Live market (Uniswap V3 0.05% QuoterV2 on Base, this block):

```
1 WETH -> 1,662.97 USDC   => ETH ~ $1,663
```

$1,663 < $1,800–$2,000, so the WETH→USDC mins are unreachable by any honest swap
of the user's input. The USDC→WETH mins, set symmetrically, are now far too
loose and score ~0.92.

### 2. The benchmark uses the literal min (no re-quote), so the swap reverts

`harness/benchmark_worker.py` (`_enrich_intents_with_manifests`) builds each
synthetic case as `new_raw_params = {**state.raw_params, **scenario["params"]}`
and never calls the solver's `quote()` to rebind `min_output_amount`. So the
literal scenario min flows into `normalize_swap_intent_params`
(`v3/manifest.py`), which sets `amountOutMinimum = min_output_amount` whenever
it is present (the `slippage_bps` fallback only applies when no min is supplied).
The Uniswap router then reverts:

```
scoreIntent REVERTED:
  0x5c0dee5d  CallFailed(uint256 index=1, bytes)
    -> 0x08c379a0  Error(string)
       -> "Too little received"
```

Reproduced on a pinned Base fork via the scoring lab (genesis solver,
`--no-requote`, the production-anchored block): WETH→USDC reverts; with a fresh
re-quote (min derived from current price) it passes at ~0.58. The only
difference is the min.

### 3. Per-case scorecard (clean genesis baseline, submission `sub_3cbe1b2ea99d`)

```
WETH_to_USDC_tiny    js=0.0000  on_chain=None  (revert: Too little received)
WETH_to_USDC_medium  js=0.0000  on_chain=None  (revert)
WETH_to_USDC_large   js=0.0000  on_chain=None  (revert)
WETH_to_USDC_xl      js=0.0000  on_chain=None  (revert)
WETH_to_DAI          js=0.0000  on_chain=None  (revert)
USDC_to_WETH_tiny    js=0.9250  on_chain=10000 (pass)
USDC_to_WETH_medium  js=0.8818  on_chain=9373  (pass)
USDC_to_WETH_large   js=0.8814  on_chain=9379  (pass)
USDC_to_WETH_xl      js=0.8253  on_chain=8579  (pass)
DAI_to_USDC          js=0.5792  on_chain=5258  (pass)
cbBTC_to_USDC        js=0.6585  on_chain=6365  (pass)
cbBTC_to_WETH        js=0.6706  on_chain=6378  (pass)
+ 10 historical orders (mixed)
aggregate your_score = 0.2874   champion_score = 0.62   score_to_beat = 0.6231
```

The failing cluster is exactly the WETH-as-input scenarios whose mins exceed the
current market. The champion's stored 0.62 cannot be reproduced on this pack — a
re-benchmark of the genesis champion today would also score ~0.29.

## Why this blocks the contest

- Max achievable on the current pack ≈ 0.5 (every winnable case at ~0.9 still
  can't offset the 5+ dead WETH-input cases).
- Adoption requires `challenger_global ≥ champion_score × 1.005 = 0.6231`.
- `0.5 < 0.6231` ⇒ **no challenger can ever be adopted**, independent of solver
  quality. This matches observed history: the highest score on record (0.7362,
  `unforkableco`, from the older pack) was never adopted, and recent live
  submissions top out at 0.11.

## Suggested fixes (any one unblocks)

1. **Re-quote the synthetic mins at benchmark time.** Bind
   `min_output_amount` from the reference solver's live `quote()` (the manifest
   already declares it `source:"quote"`, `quote_field:"suggested_min_output"`) —
   the live-order path in `api/routes/orders.py` already does exactly this. The
   synthetic benchmark path should too, so mins track current prices.
2. **Refresh the hard-coded scenario mins** to current market (or express them
   as a slippage band off a live oracle rather than absolute USDC amounts).
3. **Re-benchmark the champion on the live pack** so `score_to_beat` reflects
   the same pack challengers face, instead of a stale 0.62 from a prior pack.

Option 1 is the most robust (self-correcting as prices move) and reuses code that
already exists on the order path.

## Repro / verification

- Live price check: `QuoterV2.quoteExactInputSingle(WETH, USDC, 1e18, 500)` on
  Base → ~1,663 USDC.
- Scorecard: `GET /v1/submissions/sub_3cbe1b2ea99d/status` (clean genesis fork).
- Fork repro: scoring-lab `bench --no-requote` at the round-anchored Base pin
  reverts WETH→USDC; default re-quote passes.

Happy to share the lab scripts or open a PR for option 1.
