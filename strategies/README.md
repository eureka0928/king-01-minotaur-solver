# Strategies

This directory holds the per-DEX/per-app strategy code your solver routes
between. The reference DEX-aggregator strategy lives in
[`dex_aggregator/`](./dex_aggregator/) and is what the default `MinerSolver`
inherits from.

You can override a strategy in two ways.

## 1. Replace the whole solver behaviour (simplest)

Open [`../solver.py`](../solver.py) and edit `MinerSolver.generate_plan` (or
override anything else from `BaselineSwapSolver`):

```python
from strategies.dex_aggregator.baseline_solver import BaselineSwapSolver

class MinerSolver(BaselineSwapSolver):
    def generate_plan(self, intent, state, snapshot=None):
        # Your routing logic. Call `super().generate_plan(...)` to delegate
        # to the baseline whenever you don't have something better.
        if state.chain_id == 8453 and self._has_better_route(intent, state):
            return self._my_custom_plan(intent, state)
        return super().generate_plan(intent, state, snapshot)
```

Best when your strategy is global — for instance "always check Curve before
falling back to Uni V3 + Aerodrome".

## 2. Add a per-app strategy module (recommended for app-specific routing)

When your routing logic depends on the App being executed, create a directory
named after the app and drop a `strategy.py` in it:

```
strategies/
└── app_<your_app_id>/
    ├── __init__.py        (empty)
    └── strategy.py
```

`strategy.py` exports a `Strategy` subclass:

```python
from minotaur_subnet.sdk.strategy import Strategy
from minotaur_subnet.shared.types import ExecutionPlan

class MyAppStrategy(Strategy):
    APP_ID = "app_<your_app_id>"

    def generate_plan(self, intent, state, context) -> ExecutionPlan | None:
        # ... routing logic specific to this app ...
        return plan

STRATEGY_CLASS = MyAppStrategy
```

Then wire it into your solver in `../solver.py`:

```python
from minotaur_subnet.sdk.routing_solver import RoutingSolver
from strategies.app_my_app_id.strategy import MyAppStrategy

class MinerSolver(RoutingSolver):
    def __init__(self):
        super().__init__()
        self.register_strategy(MyAppStrategy())
```

`RoutingSolver` dispatches `generate_plan` calls to the strategy whose
`APP_ID` matches `intent.app_id`. When no strategy matches, it falls back to
the upstream baseline.

## Submitting

Whichever pattern you pick, the validator harness only cares that
`SOLVER_CLASS` is exported from `solver.py`. Test locally with the testnet
in [`subnet112/minotaur_subnet`](https://github.com/subnet112/minotaur_subnet)
before submitting your commit hash through the miner pipeline.
