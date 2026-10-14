# v0.1.0
# { "Depends": "py-genlayer:5jycge4q8k23462jtb0b9fyey1s9qz928sz2nbrd9mg4sxqg2qng" }

# AetherClaim - Decentralized Parametric Climate & Agricultural Insurance.
#
# Parametric drought cover on an authenticated plot registry. A claim is
# settled by GenLayer validators independently re-querying three independent
# meteorological pipelines and agreeing on a DISCRETE payout tier. There is no
# LLM in the settlement path: the verdict is a pure function of the observed
# rainfall, so honest validators cannot disagree except at a true bucket edge.
#
# Design constraints (each one closes a specific, previously exploited vector):
#
#   1. No arbitrary coordinates. Policies attach to a plot_id in an
#      admin-curated registry (cadastral hash, baseline, crop cycle). The
#      observation URLs are built INSIDE the contract from registry
#      coordinates; no caller-supplied URL or coordinate ever reaches a fetch.
#   2. Multi-source aggregation. Three sources. With all three answering, the
#      settled tier is their MEDIAN, and ANY caller may settle. With exactly two
#      answering (degraded mode), the settled tier is the LOWER of the two, but
#      only the POLICYHOLDER may accept it. Anyone else gets QUORUM_FAILURE, so a
#      third party cannot lock the insured into a lower tier during an outage.
#      With fewer than two answering, the result is QUORUM_FAILURE for everyone.
#   3. Sustained deviation. The tier is the MINIMUM across two consecutive
#      observation windows, so a one-window anomaly pays nothing.
#   4. Discrete payouts. Only 0 / 30 / 70 / 100 percent exist. Validators
#      compare the integer tier EXACTLY, never a tolerance band, so the money
#      moved is identical on every validator (no economic drift).
#   5. European exercise. The verdict is taken once, after the policy's FINAL
#      28-day observation windows (fixed at purchase) have matured. Nothing can
#      be re-checked daily, and a settled outcome is final.
#   7. Micro-baseline floor. A window expecting under 10 mm cannot evidence a
#      drought, so it reads 0%. Arid seasons cannot trigger catastrophic tiers.
#   8. LP orphan guard. The last LP cannot exit while policies are live, so no
#      reserved capital is left without a shareholder.
#   9. Worthless-policy guard. A term whose final window falls below the floor is
#      refused at purchase, so no premium is taken for a policy that cannot pay.
#  10. Degraded settlement is policyholder-only. The aggregate is an order
#      statistic, so a degraded (two-source) tier is never above the full
#      three-source median. That makes waiting weakly better for the insured, and
#      lets them choose: wait for the third source, or accept the lower tier now.
#      Third-party quorum failures write no state, so they cannot rate-limit the
#      policyholder's retries. Retries are open until 30 days after term end.
#   6. Cooldown + vesting + exposure caps. Approved payouts sit through a
#      24h cooldown, then vest linearly over 3 days; per-epoch underwriting is
#      capped and total liabilities may never exceed the pool.

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import genlayer as gl
from genlayer import Address, i64, u32, u256
from genlayer.storage import DynArray, TreeMap

# genvm-lint requires the bare name `allow_storage` on storage dataclasses.
allow_storage = gl.storage.allow

# --- Error classification ----------------------------------------------------
ERR_EXPECTED = "[EXPECTED]"  # deterministic business-rule rejection
ERR_EXTERNAL = "[EXTERNAL]"  # deterministic upstream problem
ERR_TRANSIENT = "[TRANSIENT]"  # non-deterministic upstream problem

# --- Protocol constants ------------------------------------------------------
DAY = 86400
STAGING_DELAY = 7 * DAY  # plot staging -> activation
EPOCH_SECONDS = 7 * DAY  # exposure accounting epoch
WINDOW_DAYS = 14  # one observation window
N_WINDOWS = 2  # consecutive windows that must both breach
DATA_LAG_DAYS = 6  # reanalysis publication lag; newest day never observed
# Micro-baseline floor: a window expecting less rain than this cannot evidence a
# drought, so its deficit is forced to 0%. Stops arid-season windows (expected
# 0-2 mm) from paying 100% on a trivial absolute shortfall.
MIN_INSURABLE_BASELINE_MM = 10
POLICY_DAYS = 90
EVAL_MIN_GAP = DAY  # minimum spacing between evaluations of one policy
COOLDOWN = DAY  # approval -> first release
VESTING = 3 * DAY  # linear vesting length
# Outage tolerance. Evaluation stays open this long after the term ends, so a
# third-party API outage cannot forfeit a valid claim.
EXPIRY_GRACE_DAYS = 30
EXPIRY_GRACE = EXPIRY_GRACE_DAYS * DAY
PREMIUM_BPS = 800  # 8.00% of coverage
MIN_COVERAGE = 10**15
MAX_POLICY_POOL_BPS = 2500  # one policy may not exceed 25% of the pool
MIN_SOURCES = 2
MIN_DEPOSIT = 10**15  # blocks first-depositor share-inflation dust attacks
MIN_EXPOSURE_CAP = MIN_COVERAGE
MAX_EXPOSURE_CAP = 100_000 * 10**18
REASONING_MAX = 400
# Validators may differ on median_deficit by this many points. Tier and
# source_count stay exact. The deficit is telemetry: only the tier moves money, so
# forecast-model updates or rounding between validator runs must not fail
# consensus and starve the policyholder.
DEFICIT_NOISE_TOLERANCE = 5
DAYS_PER_MONTH = 30  # monthly baseline -> per-window baseline scaling
SOURCE_COUNT = 3

# Discrete payout buckets: tier -> (min deficit pct, payout bps, label)
TIER_PAYOUT_BPS = {0: 0, 1: 3000, 2: 7000, 3: 10000}
# -2 is the only inconclusive code: fewer than MIN_SOURCES answered. It is an
# outage, not a verdict, and it is the only outcome that leaves a policy open.
# A split between answering sources is NOT inconclusive. It settles at the
# conservative tier (see _corroborate). The code is a consensus value, so
# validators compare it exactly and a leader cannot relabel an outage as a verdict.
TIER_LABEL = {
    -2: "QUORUM_FAILURE",
    0: "NORMAL",
    1: "MODERATE_DROUGHT",
    2: "SEVERE_DROUGHT",
    3: "CATASTROPHIC_DROUGHT",
}

# Genesis registry, seeded into storage at deployment and active immediately.
# lat/lon are degrees * 10_000. `monthly` is expected rainfall in mm for each
# calendar month Jan..Dec; the per-14-day-window baseline is derived from the
# month the window falls in. The crop cycle is (start_month, end_month), and may
# wrap the year end (Oct..Mar).
GENESIS_PLOTS = [
    ("CV-ALMOND-01", "Central Valley Almond Basin", "ALMOND", 366000, -1198000,
     [50, 45, 35, 15, 8, 2, 0, 0, 4, 15, 25, 45],
     "7c1f0a52d19b3e6a8f4b0c2d9e57a31b6f8d40c7e92a5b1d3c68f0a47e2b9d15", 11, 3),
    ("IA-CORN-01", "Iowa Corn Belt", "CORN", 420300, -938200,
     [25, 28, 55, 95, 120, 140, 115, 110, 85, 70, 50, 35],
     "b3a94e07c5d12f68a0e7b94c1d3f56a8e20c97b4d1f63a85c0e2b7194d6f3a08", 4, 10),
    ("AND-OLIVE-01", "Andalusia Olive Groves", "OLIVE", 373800, -43700,
     [60, 55, 50, 45, 30, 10, 2, 3, 25, 60, 75, 70],
     "e51d7c3a9f08b246d1a5e83c70b9f4d2a6c18e5f307b94d2c1a8e6f05b3d7a49", 10, 3),
]
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@allow_storage
@dataclass
class PlotDetails:
    name: str
    crop_type: str
    latitude_e4: i64  # degrees * 10_000
    longitude_e4: i64
    monthly_baselines_csv: str  # 12 comma-separated ints: expected mm, Jan..Dec
    cadastral_hash: str  # 64-hex digest of the official parcel boundary
    crop_cycle_start_month: u32  # 1..12
    crop_cycle_end_month: u32  # 1..12, may be < start (wraps the year)
    active: bool
    staged_at: u256
    registered_at: u256  # 0 until activated


@allow_storage
@dataclass
class PolicyRecord:
    policyholder: Address
    plot_id: str
    coverage_amount: u256
    premium_paid: u256
    start_epoch: u256  # epoch number at purchase
    end_epoch: u256  # epoch number at expiry
    start_ts: u256
    end_ts: u256
    claimed: bool
    status: str  # ACTIVE | SETTLED_PAYOUT | SETTLED_NO_PAYOUT | EXPIRED
    observation_start: u256  # first day of the 28-day window span, fixed at purchase
    observation_end: u256  # last full day inside the term, fixed at purchase
    matures_at: u256  # term end + reanalysis lag: the only moment a verdict may be taken


@allow_storage
@dataclass
class ClaimRecord:
    payout_tier: u256
    outcome: str  # TIER label, or QUORUM_FAILURE
    median_deficit: u256
    source_count: u256
    reasoning: str
    approved_payout: u256
    vested_amount: u256  # cumulative amount already released
    last_release_timestamp: u256
    cooldown_until: u256
    evaluated_at: u256
    evaluations: u256


# --- Pure, deterministic helpers (shared by leader and validators) -----------
def _deficit_pct(baseline_tenths: int, observed_tenths: int) -> int:
    if baseline_tenths <= 0:
        return 0
    if observed_tenths >= baseline_tenths:
        return 0
    return ((baseline_tenths - observed_tenths) * 100) // baseline_tenths


def _tier_for_deficit(deficit_pct: int) -> int:
    if deficit_pct >= 75:
        return 3
    if deficit_pct >= 50:
        return 2
    if deficit_pct >= 25:
        return 1
    return 0


def _parse_baselines(csv: str):
    return [int(x) for x in csv.split(",")]


def _month_in_cycle(month: int, start: int, end: int) -> bool:
    if start <= end:
        return start <= month <= end
    return month >= start or month <= end


def _valid_baselines(vals) -> bool:
    if not isinstance(vals, (list, tuple)) or len(vals) != 12:
        return False
    for v in vals:
        if isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > 1000:
            return False
    return sum(vals) > 0


def _window_baseline_tenths(monthly, w_start: datetime, w_end: datetime) -> int:
    """Expected rain (tenths of mm) for one window: the monthly baseline of the
    month the window's midpoint falls in, scaled to the window length."""
    mid = w_start + (w_end - w_start) / 2
    monthly_mm = int(monthly[mid.month - 1])
    return monthly_mm * 10 * WINDOW_DAYS // DAYS_PER_MONTH


def _fmt_e4(v: int) -> str:
    sign = "-" if v < 0 else ""
    a = abs(v)
    return f"{sign}{a // 10000}.{a % 10000:04d}"


def _window_bounds(obs_end: datetime):
    """Oldest-first list of (start_date, end_date) for each window; obs_end is
    the last observed day (inclusive)."""
    out = []
    for i in range(N_WINDOWS):
        w_end = obs_end - timedelta(days=WINDOW_DAYS * (N_WINDOWS - 1 - i))
        w_start = w_end - timedelta(days=WINDOW_DAYS - 1)
        out.append((w_start, w_end))
    return out


def _source_urls(lat_e4: int, lon_e4: int, first: datetime, last: datetime):
    lat = _fmt_e4(lat_e4)
    lon = _fmt_e4(lon_e4)
    iso_a, iso_b = first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d")
    cmp_a, cmp_b = first.strftime("%Y%m%d"), last.strftime("%Y%m%d")
    return [
        (
            "open-meteo-era5",
            "https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={lat}&longitude={lon}&start_date={iso_a}&end_date={iso_b}"
            "&daily=precipitation_sum&timezone=UTC",
        ),
        (
            "noaa-gfs-via-open-meteo",
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}&start_date={iso_a}&end_date={iso_b}"
            "&daily=precipitation_sum&models=gfs_seamless&timezone=UTC",
        ),
        (
            "nasa-power",
            "https://power.larc.nasa.gov/api/temporal/daily/point"
            f"?parameters=PRECTOTCORR&community=AG&longitude={lon}&latitude={lat}"
            f"&start={cmp_a}&end={cmp_b}&format=JSON",
        ),
    ]


def _daily_series(source: str, body: dict, days):
    """Map a source payload to {YYYY-MM-DD: tenths-of-mm int}, or None."""
    out = {}
    if source == "nasa-power":
        series = body["properties"]["parameter"]["PRECTOTCORR"]
        for d in days:
            v = series.get(d.strftime("%Y%m%d"))
            if v is None or v < 0:  # NASA POWER uses -999 for missing
                return None
            out[d.strftime("%Y-%m-%d")] = int(round(float(v) * 10))
        return out
    daily = body["daily"]
    times, vals = daily["time"], daily["precipitation_sum"]
    lookup = {t: v for t, v in zip(times, vals)}
    for d in days:
        key = d.strftime("%Y-%m-%d")
        v = lookup.get(key)
        if v is None or v < 0:
            return None
        out[key] = int(round(float(v) * 10))
    return out


def _observe_source(source: str, url: str, windows, baselines_tenths):
    """Returns (tier, deficit) for one source, or None when it is not valid."""
    try:
        res = gl.nondet.web.get(url)
        if res.status != 200:
            return None
        import json

        body = json.loads(res.body.decode("utf-8"))
        all_days = []
        for w_start, w_end in windows:
            for i in range((w_end - w_start).days + 1):
                all_days.append(w_start + timedelta(days=i))
        series = _daily_series(source, body, all_days)
        if series is None:
            return None
        deficits = []
        for idx, (w_start, w_end) in enumerate(windows):
            total = 0
            for i in range((w_end - w_start).days + 1):
                total += series[(w_start + timedelta(days=i)).strftime("%Y-%m-%d")]
            # Micro-baseline floor: an arid window cannot evidence a drought.
            if baselines_tenths[idx] < MIN_INSURABLE_BASELINE_MM * 10:
                deficits.append(0)
            else:
                deficits.append(_deficit_pct(baselines_tenths[idx], total))
        # Sustained deviation: the weakest window decides the source's verdict.
        sustained = min(deficits)
        return (_tier_for_deficit(sustained), sustained)
    except Exception:
        return None


def _canonical_reasoning(tier: int, median: int, count: int) -> str:
    """Built from committed fields only, never from leader text."""
    if tier == -2:
        return f"QUORUM_FAILURE: {count} of {SOURCE_COUNT} sources answered, {MIN_SOURCES} required; retry permitted"
    if count == SOURCE_COUNT:
        return (
            f"{count} sources answered: median tier {tier} ({TIER_LABEL[tier]}), "
            f"median sustained deficit {median}%"
        )
    return (
        f"{count} sources answered: conservative floor tier {tier} ({TIER_LABEL[tier]}), "
        f"lower sustained deficit {median}%"
    )


def _payload_ok(obj) -> bool:
    """Strict structural + bounds check of a consensus payload. Pure function:
    used by validators on the leader's output and by the contract again before
    anything is written to storage."""
    if not isinstance(obj, dict):
        return False
    # Exactly the four expected keys, so nothing unexpected is carried through.
    if set(obj.keys()) != {"tier", "median_deficit", "source_count", "reasoning"}:
        return False
    tier, med, cnt, why = obj["tier"], obj["median_deficit"], obj["source_count"], obj["reasoning"]
    for v in (tier, med, cnt):
        if isinstance(v, bool) or not isinstance(v, int):
            return False
    if tier != -2 and (tier < 0 or tier > 3):
        return False
    if med < 0 or med > 100:
        return False
    if cnt < 0 or cnt > SOURCE_COUNT:
        return False
    if not isinstance(why, str) or len(why) > REASONING_MAX:
        return False
    if any(ord(ch) < 32 or ord(ch) > 126 for ch in why):
        return False
    if tier == -2 and cnt >= MIN_SOURCES:
        return False  # a quorum failure means fewer than MIN_SOURCES answered
    if tier >= 0:
        # A settled tier needs a quorum, and the deficit must sit in its bucket.
        if cnt < MIN_SOURCES or _tier_for_deficit(med) != tier:
            return False
    return True


def _corroborate(observations):
    """Deterministic consensus over per-source (name, (tier, deficit) | None).

    Aggregation is an ORDER STATISTIC, which makes it monotone in availability:
      * fewer than MIN_SOURCES answered: QUORUM_FAILURE (-2), retry permitted.
      * all three answered: the MEDIAN tier. Two sources read at least that tier
        and two read at most it, so one faulty station in either direction cannot
        move the payout past the majority view.
      * two answered: the LOWER tier. Conservative: LPs never pay above what both
        answering sources support.

    Monotonicity: dropping a source can only lower the settled tier, never raise
    it. A degraded two-source tier is therefore never above the full median, which
    is why evaluate_claim lets only the policyholder accept it. Each source's tier
    is a non-decreasing function of its deficit, so the
    k-th order statistic of the tiers is the tier of the k-th order statistic of
    the deficits. median_deficit is therefore the same order statistic and stays
    consistent with the settled tier. A min over all three would not be monotone:
    a source going down could lift the settled tier (checked exhaustively in
    tests/test_aether_claim.py::test_availability_cannot_raise_payout).
    """
    answered = [o for _, o in observations if o is not None]
    count = len(answered)
    if count < MIN_SOURCES:
        return {
            "tier": -2,
            "median_deficit": 0,
            "source_count": count,
            "reasoning": _canonical_reasoning(-2, 0, count),
        }
    tiers = sorted(o[0] for o in answered)
    deficits = sorted(o[1] for o in answered)
    k = 1 if count == SOURCE_COUNT else 0  # median of three, lower of two
    tier, med = tiers[k], deficits[k]
    return {
        "tier": tier,
        "median_deficit": med,
        "source_count": count,
        "reasoning": _canonical_reasoning(tier, med, count),
    }


class AetherClaim(gl.contract.Contract):
    admin: Address
    pool_balance: u256  # free + reserved liquidity, excluding committed payouts
    total_liabilities: u256  # coverage reserved against live, unresolved policies
    committed_payouts: u256  # approved payouts not yet released
    max_epoch_exposure: u256
    plots: TreeMap[str, PlotDetails]
    plot_ids: DynArray[str]
    policies: TreeMap[str, PolicyRecord]
    policy_ids: DynArray[str]
    claims: TreeMap[str, ClaimRecord]
    epoch_exposure: TreeMap[str, u256]  # str(epoch) -> coverage underwritten
    lp_shares: TreeMap[str, u256]  # address hex -> vault shares
    total_lp_shares: u256
    next_policy_id: u256

    def __init__(self, max_epoch_exposure: int = 500 * 10**18):
        if not (MIN_EXPOSURE_CAP <= max_epoch_exposure <= MAX_EXPOSURE_CAP):
            raise gl.vm.UserError(f"{ERR_EXPECTED} exposure cap out of bounds")
        self.admin = gl.message.sender_address
        self.total_lp_shares = 0
        self.pool_balance = 0
        self.total_liabilities = 0
        self.committed_payouts = 0
        self.max_epoch_exposure = max_epoch_exposure
        self.next_policy_id = 1
        now = self._now()
        for pid, name, crop, lat, lon, monthly, chash, c_start, c_end in GENESIS_PLOTS:
            self.plots[pid] = PlotDetails(
                name=name,
                crop_type=crop,
                latitude_e4=lat,
                longitude_e4=lon,
                monthly_baselines_csv=",".join(str(x) for x in monthly),
                cadastral_hash=chash,
                crop_cycle_start_month=c_start,
                crop_cycle_end_month=c_end,
                active=True,
                staged_at=now,
                registered_at=now,
            )
            self.plot_ids.append(pid)

    # ------------------------------------------------------------------ utils
    def _now(self) -> int:
        # Deterministic block clock (patched inside GenVM, warp()-able in tests).
        return int(datetime.now(timezone.utc).timestamp())

    def _require_admin(self) -> None:
        if gl.message.sender_address != self.admin:
            raise gl.vm.UserError(f"{ERR_EXPECTED} only admin")

    def _plot_view(self, pid: str) -> dict:
        p = self.plots[pid]
        return {
            "plot_id": pid,
            "name": p.name,
            "crop_type": p.crop_type,
            "latitude": p.latitude_e4 / 10000,
            "longitude": p.longitude_e4 / 10000,
            "latitude_e4": int(p.latitude_e4),
            "longitude_e4": int(p.longitude_e4),
            "monthly_baselines": _parse_baselines(p.monthly_baselines_csv),
            "cadastral_hash": p.cadastral_hash,
            "crop_cycle_start_month": int(p.crop_cycle_start_month),
            "crop_cycle_end_month": int(p.crop_cycle_end_month),
            "crop_cycle": f"{MONTH_NAMES[int(p.crop_cycle_start_month) - 1]}-{MONTH_NAMES[int(p.crop_cycle_end_month) - 1]}",
            "active": bool(p.active),
            "staged_at": int(p.staged_at),
            "registered_at": int(p.registered_at),
        }

    def _policy_view(self, pid: str) -> dict:
        p = self.policies[pid]
        return {
            "policy_id": pid,
            "policyholder": p.policyholder.as_hex,
            "plot_id": p.plot_id,
            "coverage_amount": str(p.coverage_amount),
            "premium_paid": str(p.premium_paid),
            "start_epoch": int(p.start_epoch),
            "end_epoch": int(p.end_epoch),
            "start_ts": int(p.start_ts),
            "end_ts": int(p.end_ts),
            "claimed": bool(p.claimed),
            "status": p.status,
            "observation_start": int(p.observation_start),
            "observation_end": int(p.observation_end),
            "matures_at": int(p.matures_at),
        }

    def _releasable(self, pid: str, now: int) -> int:
        if pid not in self.claims:
            return 0
        c = self.claims[pid]
        if c.approved_payout == 0 or now < c.cooldown_until:
            return 0
        elapsed = min(now - int(c.cooldown_until), VESTING)
        vested = int(c.approved_payout) * elapsed // VESTING
        return max(0, vested - int(c.vested_amount))

    # ------------------------------------------------------------------ views
    @gl.public.view
    def whoami(self) -> str:
        return gl.message.sender_address.as_hex

    @gl.public.view
    def get_config(self) -> dict:
        return {
            "admin": self.admin.as_hex,
            "max_epoch_exposure": str(self.max_epoch_exposure),
            "premium_bps": PREMIUM_BPS,
            "window_days": WINDOW_DAYS,
            "windows_required": N_WINDOWS,
            "cooldown_seconds": COOLDOWN,
            "vesting_seconds": VESTING,
            "staging_delay_seconds": STAGING_DELAY,
            "min_deposit": str(MIN_DEPOSIT),
            "min_insurable_baseline_mm": MIN_INSURABLE_BASELINE_MM,
            "data_lag_days": DATA_LAG_DAYS,
            "expiry_grace_days": EXPIRY_GRACE_DAYS,
            "quorum_min_sources": MIN_SOURCES,
            "aggregation": "median of 3 answering sources; lower of 2",
            "exposure_cap_bounds": [str(MIN_EXPOSURE_CAP), str(MAX_EXPOSURE_CAP)],
            "policy_days": POLICY_DAYS,
            "epoch_seconds": EPOCH_SECONDS,
            "tier_payout_bps": {str(k): v for k, v in TIER_PAYOUT_BPS.items()},
        }

    @gl.public.view
    def get_pool(self) -> dict:
        epoch = self._now() // EPOCH_SECONDS
        used = self.epoch_exposure.get(str(epoch), 0)
        return {
            "pool_balance": str(self.pool_balance),
            "total_liabilities": str(self.total_liabilities),
            "committed_payouts": str(self.committed_payouts),
            "free_capital": str(self.pool_balance - self.total_liabilities),
            "epoch": epoch,
            "epoch_exposure": str(used),
            "max_epoch_exposure": str(self.max_epoch_exposure),
            "contract_balance": str(self.balance),
            "total_lp_shares": str(self.total_lp_shares),
            "share_price_atto": str(
                self.pool_balance * 10**18 // self.total_lp_shares if self.total_lp_shares > 0 else 10**18
            ),
        }

    @gl.public.view
    def get_plot(self, plot_id: str) -> dict:
        if plot_id not in self.plots:
            raise gl.vm.UserError(f"{ERR_EXPECTED} unknown plot")
        return self._plot_view(plot_id)

    @gl.public.view
    def list_plots(self) -> list:
        return [self._plot_view(self.plot_ids[i]) for i in range(len(self.plot_ids))]

    @gl.public.view
    def get_policy(self, policy_id: str) -> dict:
        if policy_id not in self.policies:
            raise gl.vm.UserError(f"{ERR_EXPECTED} unknown policy")
        return self._policy_view(policy_id)

    @gl.public.view
    def list_policies(self, holder_hex: str) -> list:
        want = holder_hex.lower()
        out = []
        for i in range(len(self.policy_ids)):
            pid = self.policy_ids[i]
            if want == "" or self.policies[pid].policyholder.as_hex.lower() == want:
                out.append(self._policy_view(pid))
        return out

    @gl.public.view
    def get_claim(self, policy_id: str) -> dict:
        if policy_id not in self.claims:
            return {}
        c = self.claims[policy_id]
        now = self._now()
        return {
            "policy_id": policy_id,
            "payout_tier": int(c.payout_tier),
            "outcome": c.outcome,
            "median_deficit": int(c.median_deficit),
            "source_count": int(c.source_count),
            "reasoning": c.reasoning,
            "approved_payout": str(c.approved_payout),
            "vested_amount": str(c.vested_amount),
            "releasable": str(self._releasable(policy_id, now)),
            "last_release_timestamp": int(c.last_release_timestamp),
            "cooldown_until": int(c.cooldown_until),
            "vesting_end": int(c.cooldown_until) + VESTING if c.approved_payout > 0 else 0,
            "evaluated_at": int(c.evaluated_at),
            "evaluations": int(c.evaluations),
        }

    # ------------------------------------------------------------ underwriting
    @gl.public.view
    def get_lp(self, holder_hex: str) -> dict:
        key = holder_hex.lower()
        shares = self.lp_shares.get(key, 0)
        value = shares * self.pool_balance // self.total_lp_shares if self.total_lp_shares > 0 else 0
        return {"shares": str(shares), "value": str(value), "total_shares": str(self.total_lp_shares)}

    @gl.public.write.payable
    def provide_liquidity(self) -> int:
        """ERC-4626-style deposit. Shares are priced against pool_balance BEFORE
        this deposit lands, so earlier LPs keep their accrued premiums."""
        amount = int(gl.message.value)
        if amount < MIN_DEPOSIT:
            raise gl.vm.UserError(f"{ERR_EXPECTED} deposit below minimum")
        # Accounting invariants. Both states are unreachable through the public
        # API: a burn that empties the vault always takes the pool to exactly 0,
        # and any surviving share keeps at least 1 wei of free capital. The guard
        # fails closed if an invariant is ever broken, rather than minting shares
        # against a value that no longer reconciles.
        if self.total_lp_shares == 0 and self.pool_balance != 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} vault accounting inconsistent: capital without shares")
        if self.total_lp_shares > 0 and self.pool_balance == 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} vault accounting inconsistent: shares without capital")
        if self.total_lp_shares == 0:
            shares = amount
        else:
            shares = amount * int(self.total_lp_shares) // int(self.pool_balance)
        if shares <= 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} deposit mints zero shares")
        key = gl.message.sender_address.as_hex.lower()
        self.lp_shares[key] = self.lp_shares.get(key, 0) + shares
        self.total_lp_shares += shares
        self.pool_balance += amount
        return shares

    @gl.public.write
    def withdraw_liquidity(self, shares_to_burn: int) -> int:
        """Burn shares for a pro-rata slice of the pool.

        payout = shares * pool_balance / total_shares, and additionally capped at
        the same fraction of UNRESERVED capital. The cap is what stops an LP
        exiting just before a claim is recognised and leaving the remaining LPs
        to absorb the loss: reserved capital can only leave through payouts or
        policy expiry, never through the first LP to the door."""
        key = gl.message.sender_address.as_hex.lower()
        burn = int(shares_to_burn)
        if burn <= 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} zero shares")
        if burn > self.lp_shares.get(key, 0):
            raise gl.vm.UserError(f"{ERR_EXPECTED} exceeds your shares")
        total = int(self.total_lp_shares)
        pool = int(self.pool_balance)
        # Orphan guard: the last LP cannot leave while policies are live. Burning
        # every share would strand the reserved capital with no shareholder, so
        # the next depositor would inherit it. Exits resume once liabilities clear.
        if self.total_liabilities > 0 and total - burn == 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} last LP cannot exit while policies are live")
        payout = burn * pool // total
        free = max(0, pool - int(self.total_liabilities))
        payout = min(payout, burn * free // total)
        if pool - payout < self.total_liabilities:
            raise gl.vm.UserError(f"{ERR_EXPECTED} capital reserved against live policies")
        if payout <= 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} nothing withdrawable")
        self.lp_shares[key] = self.lp_shares[key] - burn
        self.total_lp_shares -= burn
        self.pool_balance -= payout
        gl.chain.Account(gl.message.sender_address).emit_transfer(payout, on="finalized")
        return payout

    # --------------------------------------------------------- plot registry
    @gl.public.write
    def stage_plot(
        self,
        plot_id: str,
        name: str,
        crop_type: str,
        latitude_e4: int,
        longitude_e4: int,
        monthly_baselines: list,
        cadastral_hash: str,
        crop_cycle_start_month: int,
        crop_cycle_end_month: int,
    ) -> None:
        self._require_admin()
        if not plot_id or len(plot_id) > 40 or plot_id in self.plots:
            raise gl.vm.UserError(f"{ERR_EXPECTED} invalid or duplicate plot_id")
        if not (-900000 <= latitude_e4 <= 900000 and -1800000 <= longitude_e4 <= 1800000):
            raise gl.vm.UserError(f"{ERR_EXPECTED} coordinates out of range")
        baselines = [int(v) if isinstance(v, int) and not isinstance(v, bool) else -1 for v in monthly_baselines]
        if not _valid_baselines(baselines):
            raise gl.vm.UserError(f"{ERR_EXPECTED} monthly_baselines must be 12 ints in 0..1000")
        for m in (crop_cycle_start_month, crop_cycle_end_month):
            if isinstance(m, bool) or not isinstance(m, int) or m < 1 or m > 12:
                raise gl.vm.UserError(f"{ERR_EXPECTED} crop cycle month must be 1..12")
        h = cadastral_hash.lower()
        if len(h) != 64 or any(ch not in "0123456789abcdef" for ch in h):
            raise gl.vm.UserError(f"{ERR_EXPECTED} cadastral_hash must be 64 hex chars")
        self.plots[plot_id] = PlotDetails(
            name=name,
            crop_type=crop_type,
            latitude_e4=latitude_e4,
            longitude_e4=longitude_e4,
            monthly_baselines_csv=",".join(str(v) for v in baselines),
            cadastral_hash=h,
            crop_cycle_start_month=crop_cycle_start_month,
            crop_cycle_end_month=crop_cycle_end_month,
            active=False,
            staged_at=self._now(),
            registered_at=0,
        )
        self.plot_ids.append(plot_id)

    @gl.public.write
    def activate_plot(self, plot_id: str) -> None:
        self._require_admin()
        if plot_id not in self.plots:
            raise gl.vm.UserError(f"{ERR_EXPECTED} unknown plot")
        p = self.plots[plot_id]
        if p.active:
            raise gl.vm.UserError(f"{ERR_EXPECTED} already active")
        now = self._now()
        if now < p.staged_at + STAGING_DELAY:
            raise gl.vm.UserError(f"{ERR_EXPECTED} TIMELOCK staging delay not elapsed")
        p.active = True
        p.registered_at = now

    @gl.public.write
    def deactivate_plot(self, plot_id: str) -> None:
        self._require_admin()
        if plot_id not in self.plots:
            raise gl.vm.UserError(f"{ERR_EXPECTED} unknown plot")
        self.plots[plot_id].active = False

    @gl.public.write
    def set_max_epoch_exposure(self, amount: int) -> None:
        self._require_admin()
        amount = int(amount)
        if not (MIN_EXPOSURE_CAP <= amount <= MAX_EXPOSURE_CAP):
            raise gl.vm.UserError(f"{ERR_EXPECTED} exposure cap out of bounds")
        self.max_epoch_exposure = amount

    # --------------------------------------------------------------- policies
    @gl.public.write.payable
    def create_policy(self, plot_id: str, coverage_amount: int) -> str:
        coverage = int(coverage_amount)
        if gl.message.sender_address == self.admin:
            raise gl.vm.UserError(f"{ERR_EXPECTED} Admin cannot hold insurance policies")
        if plot_id not in self.plots:
            raise gl.vm.UserError(f"{ERR_EXPECTED} plot not in authenticated registry")
        if not self.plots[plot_id].active:
            raise gl.vm.UserError(f"{ERR_EXPECTED} plot not active")
        if coverage < MIN_COVERAGE:
            raise gl.vm.UserError(f"{ERR_EXPECTED} coverage below minimum")
        # Seasonality: every calendar month the coverage term touches must be
        # inside the plot's registered crop cycle, so off-season dry months
        # (where a 'drought' is just the climate) can never be insured.
        plot_rec = self.plots[plot_id]
        c_start, c_end = int(plot_rec.crop_cycle_start_month), int(plot_rec.crop_cycle_end_month)
        t_now = self._now()
        probes = [t_now + d * DAY for d in range(0, POLICY_DAYS + 1, 7)] + [t_now + POLICY_DAYS * DAY]
        for ts in probes:
            month = datetime.fromtimestamp(ts, tz=timezone.utc).month
            if not _month_in_cycle(month, c_start, c_end):
                raise gl.vm.UserError(f"{ERR_EXPECTED} coverage term extends outside the crop cycle (off-season)")
        premium = coverage * PREMIUM_BPS // 10000
        if gl.message.value != premium:
            raise gl.vm.UserError(f"{ERR_EXPECTED} premium must be exactly {premium}")

        # Premium joins the pool first, so a policy is underwritten by capital
        # that already exists on-chain.
        pool_after = self.pool_balance + premium
        if coverage * 10000 > pool_after * MAX_POLICY_POOL_BPS:
            raise gl.vm.UserError(f"{ERR_EXPECTED} exceeds single-policy concentration cap")
        if self.total_liabilities + coverage > pool_after:
            raise gl.vm.UserError(f"{ERR_EXPECTED} insufficient pool capital (solvency)")
        now = self._now()
        epoch = now // EPOCH_SECONDS
        ekey = str(epoch)
        end_ts = now + POLICY_DAYS * DAY
        # Observation windows are the FINAL 28 days of the term, fixed here at
        # purchase. observation_end is the last full day inside the term. The
        # verdict may only be taken once data for that day is published.
        obs_end_ts = end_ts - end_ts % DAY - DAY
        obs_start_ts = obs_end_ts - (WINDOW_DAYS * N_WINDOWS - 1) * DAY
        matures_ts = end_ts + DATA_LAG_DAYS * DAY
        # Worthless-policy guard. A window below the micro-baseline floor reads 0%
        # at settlement, so a term containing one can only ever pay nothing. The
        # check uses the same window math as settlement: the window's expected
        # rain (monthly x 14/30), not the raw monthly figure. A monthly check
        # would sell 10-21 mm months whose windows are under the floor.
        monthly = _parse_baselines(plot_rec.monthly_baselines_csv)
        for w_start, w_end in _window_bounds(datetime.fromtimestamp(obs_end_ts, tz=timezone.utc)):
            if _window_baseline_tenths(monthly, w_start, w_end) < MIN_INSURABLE_BASELINE_MM * 10:
                raise gl.vm.UserError(
                    f"{ERR_EXPECTED} Term uninsurable: observation window falls in arid month (< 10mm baseline)"
                )
        used = self.epoch_exposure.get(ekey, 0)
        if used + coverage > self.max_epoch_exposure:
            raise gl.vm.UserError(f"{ERR_EXPECTED} epoch exposure cap exceeded")

        self.pool_balance = pool_after
        self.total_liabilities += coverage
        self.epoch_exposure[ekey] = used + coverage

        pid = f"P-{int(self.next_policy_id)}"
        self.next_policy_id += 1
        self.policies[pid] = PolicyRecord(
            policyholder=gl.message.sender_address,
            plot_id=plot_id,
            coverage_amount=coverage,
            premium_paid=premium,
            start_epoch=epoch,
            end_epoch=end_ts // EPOCH_SECONDS,
            start_ts=now,
            end_ts=end_ts,
            claimed=False,
            status="ACTIVE",
            observation_start=obs_start_ts,
            observation_end=obs_end_ts,
            matures_at=matures_ts,
        )
        self.policy_ids.append(pid)
        return pid

    @gl.public.write
    def expire_policy(self, policy_id: str) -> None:
        """Release the reserve of an unclaimed policy once the grace period ends.

        The grace period is the retry window. Quorum-failure retries are allowed
        until term end + EXPIRY_GRACE, and evaluate_claim closes at exactly the
        same instant. Expiry opens immediately after, so no separate hold is
        needed, and LP capital is released as soon as the window is gone."""
        if policy_id not in self.policies:
            raise gl.vm.UserError(f"{ERR_EXPECTED} unknown policy")
        pol = self.policies[policy_id]
        if pol.status != "ACTIVE":
            raise gl.vm.UserError(f"{ERR_EXPECTED} policy not active")
        if self._now() <= pol.end_ts + EXPIRY_GRACE:
            raise gl.vm.UserError(f"{ERR_EXPECTED} TIMELOCK still inside claim grace period")
        pol.status = "EXPIRED"
        self.total_liabilities -= pol.coverage_amount

    # ----------------------------------------------------------------- claims
    @gl.public.write
    def evaluate_claim(self, policy_id: str) -> dict:
        """Settle a policy's claim against its fixed observation windows.

        Timing assumptions (stated for auditors):
          * Windows are fixed at purchase. Evaluation opens at matures_at and
            closes at end_ts + EXPIRY_GRACE. The closing instant equals the
            opening instant of expiry, so the two cannot both be open.
          * Who may settle. With all three sources answering, any caller may
            settle at the median. With exactly two answering, only the
            policyholder may settle, at the lower tier. For anyone else the
            result is QUORUM_FAILURE and NO state is written, so a third party
            can neither lock a lower tier nor consume the policyholder's retry
            rate limit.
          * Only QUORUM_FAILURE leaves a policy ACTIVE. The policyholder's own
            retries are allowed at most once per EVAL_MIN_GAP.
          * Every settled outcome (tier 0 or a payout) is final on first
            settlement. A second evaluation is refused.
          * A degraded tier is never above the full three-source median, so
            waiting for the third source is weakly better for the insured. The
            insured may still accept the degraded tier to settle sooner.
          * Data stability. The contract assumes the historical values inside the
            fixed windows do not change during the grace period. A source that
            revises a value can change the result of a quorum-failure retry. The
            first settlement is still final.
        """
        if policy_id not in self.policies:
            raise gl.vm.UserError(f"{ERR_EXPECTED} unknown policy")
        pol = self.policies[policy_id]
        if pol.status != "ACTIVE" or pol.claimed:
            raise gl.vm.UserError(f"{ERR_EXPECTED} policy not claimable")
        now = self._now()
        if now < int(pol.matures_at):
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} TIMELOCK not matured: evaluation opens at {int(pol.matures_at)}"
            )
        if now > int(pol.end_ts) + EXPIRY_GRACE:
            raise gl.vm.UserError(f"{ERR_EXPECTED} policy expired")
        prior = self.claims.get(policy_id, None)
        if prior is not None and now < int(prior.evaluated_at) + EVAL_MIN_GAP:
            raise gl.vm.UserError(f"{ERR_EXPECTED} TIMELOCK evaluated too recently")

        obs_end = datetime.fromtimestamp(int(pol.observation_end), tz=timezone.utc)
        obs_start = datetime.fromtimestamp(int(pol.observation_start), tz=timezone.utc)
        plot = self.plots[pol.plot_id]
        lat, lon = int(plot.latitude_e4), int(plot.longitude_e4)
        monthly = _parse_baselines(plot.monthly_baselines_csv)
        windows = _window_bounds(obs_end)
        baselines_tenths = [_window_baseline_tenths(monthly, ws, we) for ws, we in windows]
        urls = _source_urls(lat, lon, obs_start, obs_end)

        def leader_fn():
            obs = [(n, _observe_source(n, u, windows, baselines_tenths)) for n, u in urls]
            return _corroborate(obs)

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False  # leader error: force rotation, never ratify
            theirs = leaders_res.calldata
            if not _payload_ok(theirs):
                return False
            mine = leader_fn()
            if not _payload_ok(mine):
                return False
            # EXACT equality of what the contract acts on: tier (the money) and
            # source_count (who may settle: degraded two-source results are
            # policyholder-only, so a leader must not claim three answering
            # sources when only two did). median_deficit is telemetry, compared
            # within DEFICIT_NOISE_TOLERANCE. _payload_ok has already checked
            # structure and int types, before any cast.
            return (
                int(theirs["tier"]) == int(mine["tier"])
                and int(theirs["source_count"]) == int(mine["source_count"])
                and abs(int(theirs["median_deficit"]) - int(mine["median_deficit"]))
                <= DEFICIT_NOISE_TOLERANCE
            )

        res = gl.vm.run_nondet(leader_fn, validator_fn)
        if not _payload_ok(res):
            raise gl.vm.UserError(f"{ERR_EXTERNAL} malformed consensus payload")
        tier = int(res["tier"])

        count = int(res["source_count"])
        is_holder = gl.message.sender_address == pol.policyholder
        if tier == -2 or (count < SOURCE_COUNT and not is_holder):
            if not is_holder:
                # Third-party call during an outage. Do not settle and do not
                # write state: no lower-tier lock, no consumed retry slot.
                return {
                    "policy_id": policy_id,
                    "settled": False,
                    "outcome": TIER_LABEL[-2],
                    "payout_tier": 0,
                    "approved_payout": "0",
                    "source_count": count,
                    "reasoning": (
                        f"QUORUM_FAILURE: {count} of {SOURCE_COUNT} sources answered; "
                        "degraded settlement is policyholder-only; retry permitted"
                    ),
                }

        evals = (int(prior.evaluations) if prior is not None else 0) + 1
        payout = 0 if tier <= 0 else int(pol.coverage_amount) * TIER_PAYOUT_BPS[tier] // 10000
        self.claims[policy_id] = ClaimRecord(
            payout_tier=max(tier, 0),
            outcome=TIER_LABEL[tier],
            median_deficit=int(res["median_deficit"]),
            source_count=int(res["source_count"]),
            reasoning=_canonical_reasoning(tier, int(res["median_deficit"]), int(res["source_count"])),
            approved_payout=payout,
            vested_amount=0,
            last_release_timestamp=0,
            cooldown_until=now + COOLDOWN if payout > 0 else 0,
            evaluated_at=now,
            evaluations=evals,
        )
        if tier == -2:
            # Quorum failure: an outage, not a verdict. Stay ACTIVE for a retry.
            return self.get_claim(policy_id)
        # Settled. Release the reserve and close the policy.
        self.total_liabilities -= pol.coverage_amount
        if payout > 0:
            pol.claimed = True
            pol.status = "SETTLED_PAYOUT"
            self.pool_balance -= payout
            self.committed_payouts += payout
        else:
            pol.status = "SETTLED_NO_PAYOUT"
        return self.get_claim(policy_id)

    @gl.public.write
    def release_payout(self, policy_id: str) -> int:
        if policy_id not in self.claims:
            raise gl.vm.UserError(f"{ERR_EXPECTED} no claim")
        c = self.claims[policy_id]
        if c.approved_payout == 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} no approved payout")
        now = self._now()
        if now < c.cooldown_until:
            raise gl.vm.UserError(f"{ERR_EXPECTED} TIMELOCK verification cooldown active")
        amount = self._releasable(policy_id, now)
        if amount <= 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} nothing vested yet")
        # Effects before the outbound transfer.
        c.vested_amount = c.vested_amount + amount
        c.last_release_timestamp = now
        self.committed_payouts -= amount
        holder = self.policies[policy_id].policyholder
        gl.chain.Account(holder).emit_transfer(amount, on="finalized")
        return amount
