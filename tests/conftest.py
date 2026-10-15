"""Shared helpers for AetherClaim direct-mode tests.

Direct mode runs the leader function in-process. Validator logic is exercised
explicitly through ``direct_vm.run_validator`` with swapped mocks.
"""

import hashlib
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
MATURITY_LAG_DAYS = 10
# Evaluation opens 10 days after the last observed day has ended.
MATURITY = T0 + timedelta(days=POLICY_DAYS + MATURITY_LAG_DAYS)

# Test-only plots (staged in the fixture). FLAT has a 14.0 mm window baseline in
# every month, so the mm values below are exact tier inputs.
TEST_PLOT = "TEST-FLAT-01"
ARID_PLOT = "ARID-02"  # 2 mm/month -> 0.9 mm per window, below the 10 mm floor
SEASONAL_PLOT = "SEASONAL-03"  # March 30 mm, April 60 mm: windows straddle months
FLAT = [30] * 12
ARID = [2] * 12
SEASONAL = [30, 30, 30, 60, 30, 30, 30, 30, 30, 30, 30, 30]
# An arid month inside one of the two final windows: purchase must revert.
ARID_MAR_PLOT = "ARID-MAR-04"  # March 2 mm
ARID_APR_PLOT = "ARID-APR-05"  # April 2 mm
ARID_MAR = [30, 30, 2, 30, 30, 30, 30, 30, 30, 30, 30, 30]
ARID_APR = [30, 30, 30, 2, 30, 30, 30, 30, 30, 30, 30, 30]
PLOT = TEST_PLOT
# Extra flat plots, so tests that need several concurrent policies can place each
# on its own plot (one live coverage window per plot is a contract rule).
EXTRA_PLOTS = ["TEST-FLAT-02", "TEST-FLAT-03", "TEST-FLAT-04", "TEST-FLAT-05"]
TEST_STATION = "TEST0000001"
TEST_MAX_COVERAGE = 1000 * 10**18
# Term end plus the 30-day outage grace: past this, the reserve is releasable.
EXPIRED_AT = T0 + timedelta(days=POLICY_DAYS + 31)

# Observed mm per 14-day window against the 14.0 mm FLAT baseline.
NORMAL, TIER1, TIER2, TIER3 = 14.0, 10.0, 5.0, 2.0

URL_OM = r"archive-api\.open-meteo\.com"
URL_NASA = r"power\.larc\.nasa\.gov"
URL_NCEI = r"www\.ncei\.noaa\.gov"
URL_MANIFEST = r"raw\.githubusercontent\.com"

# Genesis registry, restated independently of the contract. verify_plot_provenance
# compares the on-chain record with this manifest, so a drift in either breaks
# test_genesis_plots_verify_against_independent_manifest.
GENESIS_MAX_COVERAGE = 50 * 10**18
GENESIS = {
    "CV-ALMOND-01": ("Central Valley Almond Basin", "ALMOND", 366000, -1198000,
                     [50, 45, 35, 15, 8, 2, 0, 0, 4, 15, 25, 45], 11, 3, "USW00093193"),
    "IA-CORN-01": ("Iowa Corn Belt", "CORN", 420300, -938200,
                   [25, 28, 55, 95, 120, 140, 115, 110, 85, 70, 50, 35], 4, 10, "USC00130200"),
    "AND-OLIVE-01": ("Andalusia Olive Groves", "OLIVE", 373800, -43700,
                     [60, 55, 50, 45, 30, 10, 2, 3, 25, 60, 75, 70], 10, 3, "SP000008410"),
}

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


def _ncei_body(obs_end, w1, w2):
    """NOAA NCEI GHCN-Daily rows. Whole-window totals sit on each window's first
    day, in mm, as strings (the service's own format)."""
    days = _days(obs_end)
    vals = [0.0] * 28
    vals[0], vals[14] = w1, w2
    return json.dumps(
        [{"DATE": d.strftime("%Y-%m-%d"), "STATION": TEST_STATION, "PRCP": f"{v:.1f}"} for d, v in zip(days, vals)]
    )


def _nasa_body(obs_end, w1, w2):
    days = _days(obs_end)
    vals = [0.0] * 28
    vals[0], vals[14] = w1, w2
    series = {d.strftime("%Y%m%d"): v for d, v in zip(days, vals)}
    return json.dumps({"properties": {"parameter": {"PRECTOTCORR": series}}})


def mock_sources(vm, om, ncei=_SAME, nasa=_SAME, obs_end=OBS_END):
    """Mock the three sources for one policy's fixed observation span.

    Each argument is (window1_mm, window2_mm), or None for an outage. ncei and
    nasa default to the same readings as om."""
    if ncei is _SAME:
        ncei = om
    if nasa is _SAME:
        nasa = om
    vm.clear_mocks()
    fail = {"status": 500, "body": ""}
    vm.mock_web(URL_OM, {"status": 200, "body": _om_body(obs_end, *om)} if om else fail)
    vm.mock_web(URL_NCEI, {"status": 200, "body": _ncei_body(obs_end, *ncei)} if ncei else fail)
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


# ------------------------------------------------------- registry / provenance
def cadastral_digest(plot_id, lat_e4, lon_e4, monthly, c_start, c_end, max_cov, station) -> str:
    """Independent implementation of the contract's cadastral commitment."""
    rec = "|".join(
        [
            "AETHERCLAIM-CADASTRE-V1",
            plot_id,
            str(lat_e4),
            str(lon_e4),
            ",".join(str(v) for v in monthly),
            str(c_start),
            str(c_end),
            str(max_cov),
            station,
        ]
    )
    return hashlib.sha256(rec.encode()).hexdigest()


def manifest_record(plot_id, lat_e4, lon_e4, monthly, c_start, c_end, max_cov, station, holder_hex):
    return {
        "cadastral_hash": cadastral_digest(plot_id, lat_e4, lon_e4, monthly, c_start, c_end, max_cov, station),
        "latitude_e4": lat_e4,
        "longitude_e4": lon_e4,
        "monthly_baselines": list(monthly),
        "crop_cycle_start_month": c_start,
        "crop_cycle_end_month": c_end,
        "max_plot_coverage": str(max_cov),
        "ghcnd_station": station,
        "eligible_policyholder": holder_hex.lower(),
    }


def manifest_body(records: dict) -> str:
    return json.dumps({"schema": "AETHERCLAIM-CADASTRE-V1", "plots": records})


def mock_manifest(vm, records: dict, status=200) -> None:
    vm.clear_mocks()
    vm.mock_web(URL_MANIFEST, {"status": status, "body": manifest_body(records) if status == 200 else ""})


def stage(c, pid, monthly, holder_hex, max_cov=TEST_MAX_COVERAGE, station=TEST_STATION, c_start=1, c_end=12):
    """Stage a test plot with a hash that commits to the record, as required."""
    digest = cadastral_digest(pid, 300000, -1000000, monthly, c_start, c_end, max_cov, station)
    c.stage_plot(pid, f"Test {pid}", "TEST", 300000, -1000000, monthly, digest, c_start, c_end, max_cov, holder_hex, station)
    return manifest_record(pid, 300000, -1000000, monthly, c_start, c_end, max_cov, station, holder_hex)


def genesis_records(holder_hex: str) -> dict:
    out = {}
    for pid, (_n, _crop, lat, lon, monthly, c_start, c_end, station) in GENESIS.items():
        out[pid] = manifest_record(pid, lat, lon, monthly, c_start, c_end, GENESIS_MAX_COVERAGE, station, holder_hex)
    return out
