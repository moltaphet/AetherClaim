"""Shared helpers for AetherClaim direct-mode tests.

Direct mode runs the leader function in-process. Validator logic is exercised
explicitly through ``direct_vm.run_validator`` with swapped mocks.
"""

import json
import os
from datetime import datetime, timedelta, timezone

# The pinned runner (py-genlayer:5jycge...) ships in the v0.6.0-rc5 GenVM tree.
os.environ.setdefault("GENVM_VERSION", "v0.6.0-rc5")

CONTRACT = "contracts/aether_claim.py"
ATTO = 10**18
DAY = 86400
HASH = "c" * 64

# Clock: every policy in the suite is bought at T0 unless a test says otherwise.
T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
POLICY_DAYS = 90
OBS_END = T0 + timedelta(days=POLICY_DAYS - 1)  # last full day inside the term
OBS_START = OBS_END - timedelta(days=27)  # first day of the 28-day span
MATURITY = T0 + timedelta(days=POLICY_DAYS + 6)  # term end + 6-day reanalysis lag

# Test-only plots (staged in the fixture). FLAT has a 14.0 mm window baseline in
# every month, so the mm values below are exact tier inputs.
TEST_PLOT = "TEST-FLAT-01"
ARID_PLOT = "ARID-02"  # 2 mm/month -> 0.9 mm per window, below the 10 mm floor
SEASONAL_PLOT = "SEASONAL-03"  # March 30 mm, April 60 mm: windows straddle months
FLAT = [30] * 12
ARID = [2] * 12
SEASONAL = [30, 30, 30, 60, 30, 30, 30, 30, 30, 30, 30, 30]
PLOT = TEST_PLOT

# Observed mm per 14-day window against the 14.0 mm FLAT baseline.
NORMAL, TIER1, TIER2, TIER3 = 14.0, 10.0, 5.0, 2.0

URL_OM = r"archive-api\.open-meteo\.com"
URL_GFS = r"api\.open-meteo\.com/v1/forecast"
URL_NASA = r"power\.larc\.nasa\.gov"

_SAME = object()


def warp_to(vm, dt: datetime) -> None:
    vm.warp(dt.strftime("%Y-%m-%dT%H:%M:%SZ"))


def _days(obs_end: datetime):
    return [obs_end - timedelta(days=27 - i) for i in range(28)]


def _om_body(obs_end, w1, w2):
    days = _days(obs_end)
    vals = [0.0] * 28
    vals[0], vals[14] = w1, w2  # whole-window totals on each window's first day
    return json.dumps(
        {"daily": {"time": [d.strftime("%Y-%m-%d") for d in days], "precipitation_sum": vals}}
    )


def _nasa_body(obs_end, w1, w2):
    days = _days(obs_end)
    vals = [0.0] * 28
    vals[0], vals[14] = w1, w2
    series = {d.strftime("%Y%m%d"): v for d, v in zip(days, vals)}
    return json.dumps({"properties": {"parameter": {"PRECTOTCORR": series}}})


def mock_sources(vm, om, gfs=_SAME, nasa=_SAME, obs_end=OBS_END):
    """Mock the three sources for one policy's fixed observation span.

    Each argument is (window1_mm, window2_mm), or None for an outage. gfs and
    nasa default to the same readings as om."""
    if gfs is _SAME:
        gfs = om
    if nasa is _SAME:
        nasa = om
    vm.clear_mocks()
    fail = {"status": 500, "body": ""}
    vm.mock_web(URL_OM, {"status": 200, "body": _om_body(obs_end, *om)} if om else fail)
    vm.mock_web(URL_GFS, {"status": 200, "body": _om_body(obs_end, *gfs)} if gfs else fail)
    vm.mock_web(URL_NASA, {"status": 200, "body": _nasa_body(obs_end, *nasa)} if nasa else fail)


def uniform(vm, mm, obs_end=OBS_END):
    mock_sources(vm, (mm, mm), (mm, mm), (mm, mm), obs_end=obs_end)


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
