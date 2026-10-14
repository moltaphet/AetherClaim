"""Shared helpers for AetherClaim direct-mode tests.

Direct mode runs the leader function in-process. Validator logic is exercised
explicitly through ``direct_vm.run_validator`` with swapped mocks, which is how
the adversarial suite proves validators reject drifted / uncorroborated leaders.
"""

import json
import os
from datetime import datetime, timedelta, timezone

# The pinned runner (py-genlayer:5jycge...) ships in the v0.6.0-rc5 GenVM tree.
os.environ.setdefault("GENVM_VERSION", "v0.6.0-rc5")

CONTRACT = "contracts/aether_claim.py"
ATTO = 10**18
DAY = 86400
T0 = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)

# Central Valley baseline = 14 mm per 14-day window (140 tenths).
PLOT = "CV-ALMOND-01"
BASELINE_MM = 14

# Total mm in a window that lands inside each bucket for a 14 mm baseline.
NORMAL, TIER1, TIER2, TIER3 = 14.0, 10.0, 5.0, 2.0

URL_OM = r"archive-api\.open-meteo\.com"
URL_GFS = r"api\.open-meteo\.com/v1/forecast"
URL_NASA = r"power\.larc\.nasa\.gov"


def warp_to(vm, dt: datetime) -> None:
    vm.warp(dt.strftime("%Y-%m-%dT%H:%M:%SZ"))


def obs_end_for(now: datetime) -> datetime:
    """Mirror of the contract's observation anchor: lagged, day-aligned."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=6)


def _series(now: datetime, w1: float, w2: float):
    end = obs_end_for(now)
    days = [end - timedelta(days=27 - i) for i in range(28)]
    vals = [0.0] * 28
    vals[0] = w1  # whole older-window total on its first day
    vals[14] = w2
    return days, vals


def _om_body(now, w1, w2):
    days, vals = _series(now, w1, w2)
    return json.dumps(
        {"daily": {"time": [d.strftime("%Y-%m-%d") for d in days], "precipitation_sum": vals}}
    )


def _nasa_body(now, w1, w2):
    days, vals = _series(now, w1, w2)
    return json.dumps(
        {"properties": {"parameter": {"PRECTOTCORR": {d.strftime("%Y%m%d"): v for d, v in zip(days, vals)}}}}
    )


def mock_sources(vm, now, om=(NORMAL, NORMAL), gfs=None, nasa=None):
    """Mock the three sources. Each arg is (w1_mm, w2_mm) or None for an outage.
    gfs / nasa default to the same readings as `om`."""
    gfs = om if gfs is None and gfs is not False else gfs
    nasa = om if nasa is None else nasa
    vm.clear_mocks()
    fail = {"status": 500, "body": ""}
    vm.mock_web(URL_OM, {"status": 200, "body": _om_body(now, *om)} if om else fail)
    vm.mock_web(URL_GFS, {"status": 200, "body": _om_body(now, *gfs)} if gfs else fail)
    vm.mock_web(URL_NASA, {"status": 200, "body": _nasa_body(now, *nasa)} if nasa else fail)


def uniform(vm, now, mm):
    mock_sources(vm, now, om=(mm, mm), gfs=(mm, mm), nasa=(mm, mm))


def fund(vm, who, amount=10_000 * ATTO):
    vm.deal(who, amount)


def provide(c, vm, who, amount=100 * ATTO):
    fund(vm, who)
    vm.sender = who
    vm.value = amount
    c.provide_liquidity()
    vm.value = 0


def buy(c, vm, who, coverage=10 * ATTO, plot=PLOT):
    fund(vm, who)
    vm.sender = who
    vm.value = coverage * 800 // 10000
    pid = c.create_policy(plot, coverage)
    vm.value = 0
    return pid


def eval_time(days_after_start: int = 40) -> datetime:
    return T0 + timedelta(days=days_after_start, hours=3)
