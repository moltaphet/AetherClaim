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
#   2. Multi-source corroboration. Three independent sources; >= 2 must answer
#      and ALL answering sources must land in the same tier, otherwise the
#      round is INCONCLUSIVE and pays nothing.
#   3. Sustained deviation. The tier is the MINIMUM across two consecutive
#      observation windows, so a one-window anomaly pays nothing.
#   4. Discrete payouts. Only 0 / 30 / 70 / 100 percent exist. Validators
#      compare the integer tier EXACTLY, never a tolerance band, so the money
#      moved is identical on every validator (no economic drift).
#   5. Anti-adverse-selection. Observation windows must lie entirely inside
#      the coverage period, so a policy cannot be bought after the drought.
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
STAGING_DELAY = DAY  # plot staging -> activation
EPOCH_SECONDS = 7 * DAY  # exposure accounting epoch
WINDOW_DAYS = 14  # one observation window
N_WINDOWS = 2  # consecutive windows that must both breach
DATA_LAG_DAYS = 6  # reanalysis publication lag; newest day never observed
POLICY_DAYS = 90
EVAL_MIN_GAP = DAY  # minimum spacing between evaluations of one policy
COOLDOWN = DAY  # approval -> first release
VESTING = 3 * DAY  # linear vesting length
EXPIRY_GRACE = 14 * DAY  # evaluation still allowed after end_ts
PREMIUM_BPS = 800  # 8.00% of coverage
MIN_COVERAGE = 10**15
MAX_POLICY_POOL_BPS = 2500  # one policy may not exceed 25% of the pool
MIN_SOURCES = 2
SOURCE_COUNT = 3

# Discrete payout buckets: tier -> (min deficit pct, payout bps, label)
TIER_PAYOUT_BPS = {0: 0, 1: 3000, 2: 7000, 3: 10000}
TIER_LABEL = {
    -1: "INCONCLUSIVE",
    0: "NORMAL",
    1: "MODERATE_DROUGHT",
    2: "SEVERE_DROUGHT",
    3: "CATASTROPHIC_DROUGHT",
}

# Genesis registry, seeded into storage at deployment and active immediately.
# lat/lon are degrees * 10_000; baseline is mm of rain expected per 14-day window.
GENESIS_PLOTS = [
    ("CV-ALMOND-01", "Central Valley Almond Basin", "ALMOND", 366000, -1198000, 14,
     "7c1f0a52d19b3e6a8f4b0c2d9e57a31b6f8d40c7e92a5b1d3c68f0a47e2b9d15", "Feb-Nov"),
    ("IA-CORN-01", "Iowa Corn Belt", "CORN", 420300, -938200, 34,
     "b3a94e07c5d12f68a0e7b94c1d3f56a8e20c97b4d1f63a85c0e2b7194d6f3a08", "Apr-Oct"),
    ("AND-OLIVE-01", "Andalusia Olive Groves", "OLIVE", 373800, -43700, 21,
     "e51d7c3a9f08b246d1a5e83c70b9f4d2a6c18e5f307b94d2c1a8e6f05b3d7a49", "Oct-Mar"),
]


@allow_storage
@dataclass
class PlotDetails:
    name: str
    crop_type: str
    latitude_e4: i64  # degrees * 10_000
    longitude_e4: i64
    baseline_rainfall_mm: u32  # expected mm per 14-day observation window
    cadastral_hash: str  # 64-hex digest of the official parcel boundary
    crop_cycle: str
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
    status: str  # ACTIVE | CLAIM_APPROVED | EXPIRED


@allow_storage
@dataclass
class ClaimRecord:
    payout_tier: u256
    outcome: str  # TIER label or INCONCLUSIVE
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


def _fmt_e4(v: int) -> str:
    sign = "-" if v < 0 else ""
    a = abs(v)
    return f"{sign}{a // 10000}.{a % 10000:04d}"


def _median(vals):
    s = sorted(vals)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) // 2


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


def _observe_source(source: str, url: str, windows, baseline_tenths: int):
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
        for w_start, w_end in windows:
            total = 0
            for i in range((w_end - w_start).days + 1):
                total += series[(w_start + timedelta(days=i)).strftime("%Y-%m-%d")]
            deficits.append(_deficit_pct(baseline_tenths, total))
        # Sustained deviation: the weakest window decides the source's verdict.
        sustained = min(deficits)
        return (_tier_for_deficit(sustained), sustained)
    except Exception:
        return None


def _corroborate(observations):
    """Deterministic consensus over per-source (name, (tier, deficit)|None).

    Pure function so it is unit-testable and identical on every validator."""
    valid = [(n, o) for n, o in observations if o is not None]
    if len(valid) < MIN_SOURCES:
        return {
            "tier": -1,
            "median_deficit": 0,
            "source_count": len(valid),
            "reasoning": f"INCONCLUSIVE: {len(valid)}/{len(observations)} sources valid, "
            f"need {MIN_SOURCES}",
        }
    tiers = sorted({o[0] for _, o in valid})
    deficits = [o[1] for _, o in valid]
    med = _median(deficits)
    if len(tiers) != 1:
        detail = ", ".join(f"{n}={o[0]}" for n, o in valid)
        return {
            "tier": -1,
            "median_deficit": med,
            "source_count": len(valid),
            "reasoning": f"INCONCLUSIVE: sources disagree on tier ({detail})",
        }
    tier = tiers[0]
    return {
        "tier": tier,
        "median_deficit": med,
        "source_count": len(valid),
        "reasoning": f"{len(valid)} sources agree on tier {tier} "
        f"({TIER_LABEL[tier]}), median sustained deficit {med}%",
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
    lp_deposits: TreeMap[str, u256]  # address hex -> principal supplied
    next_policy_id: u256

    def __init__(self, max_epoch_exposure: int = 500 * 10**18):
        self.admin = gl.message.sender_address
        self.pool_balance = 0
        self.total_liabilities = 0
        self.committed_payouts = 0
        self.max_epoch_exposure = max_epoch_exposure
        self.next_policy_id = 1
        now = self._now()
        for pid, name, crop, lat, lon, base, chash, cycle in GENESIS_PLOTS:
            self.plots[pid] = PlotDetails(
                name=name,
                crop_type=crop,
                latitude_e4=lat,
                longitude_e4=lon,
                baseline_rainfall_mm=base,
                cadastral_hash=chash,
                crop_cycle=cycle,
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
            "baseline_rainfall_mm": int(p.baseline_rainfall_mm),
            "cadastral_hash": p.cadastral_hash,
            "crop_cycle": p.crop_cycle,
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
    @gl.public.write.payable
    def provide_liquidity(self) -> None:
        if gl.message.value == 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} zero deposit")
        key = gl.message.sender_address.as_hex
        self.lp_deposits[key] = self.lp_deposits.get(key, 0) + gl.message.value
        self.pool_balance += gl.message.value

    @gl.public.write
    def withdraw_liquidity(self, amount: int) -> None:
        key = gl.message.sender_address.as_hex
        amount = int(amount)
        if amount <= 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} zero amount")
        if amount > self.lp_deposits.get(key, 0):
            raise gl.vm.UserError(f"{ERR_EXPECTED} exceeds your principal")
        if amount > self.pool_balance - self.total_liabilities:
            raise gl.vm.UserError(f"{ERR_EXPECTED} capital reserved against live policies")
        self.lp_deposits[key] = self.lp_deposits[key] - amount
        self.pool_balance -= amount
        gl.chain.Account(gl.message.sender_address).emit_transfer(amount, on="finalized")

    # --------------------------------------------------------- plot registry
    @gl.public.write
    def stage_plot(
        self,
        plot_id: str,
        name: str,
        crop_type: str,
        latitude_e4: int,
        longitude_e4: int,
        baseline_rainfall_mm: int,
        cadastral_hash: str,
        crop_cycle: str,
    ) -> None:
        self._require_admin()
        if not plot_id or len(plot_id) > 40 or plot_id in self.plots:
            raise gl.vm.UserError(f"{ERR_EXPECTED} invalid or duplicate plot_id")
        if not (-900000 <= latitude_e4 <= 900000 and -1800000 <= longitude_e4 <= 1800000):
            raise gl.vm.UserError(f"{ERR_EXPECTED} coordinates out of range")
        if baseline_rainfall_mm <= 0 or baseline_rainfall_mm > 1000:
            raise gl.vm.UserError(f"{ERR_EXPECTED} baseline out of range")
        h = cadastral_hash.lower()
        if len(h) != 64 or any(ch not in "0123456789abcdef" for ch in h):
            raise gl.vm.UserError(f"{ERR_EXPECTED} cadastral_hash must be 64 hex chars")
        self.plots[plot_id] = PlotDetails(
            name=name,
            crop_type=crop_type,
            latitude_e4=latitude_e4,
            longitude_e4=longitude_e4,
            baseline_rainfall_mm=baseline_rainfall_mm,
            cadastral_hash=h,
            crop_cycle=crop_cycle,
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
        self.max_epoch_exposure = int(amount)

    # --------------------------------------------------------------- policies
    @gl.public.write.payable
    def create_policy(self, plot_id: str, coverage_amount: int) -> str:
        coverage = int(coverage_amount)
        if plot_id not in self.plots:
            raise gl.vm.UserError(f"{ERR_EXPECTED} plot not in authenticated registry")
        if not self.plots[plot_id].active:
            raise gl.vm.UserError(f"{ERR_EXPECTED} plot not active")
        if coverage < MIN_COVERAGE:
            raise gl.vm.UserError(f"{ERR_EXPECTED} coverage below minimum")
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
        used = self.epoch_exposure.get(ekey, 0)
        if used + coverage > self.max_epoch_exposure:
            raise gl.vm.UserError(f"{ERR_EXPECTED} epoch exposure cap exceeded")

        self.pool_balance = pool_after
        self.total_liabilities += coverage
        self.epoch_exposure[ekey] = used + coverage

        pid = f"P-{int(self.next_policy_id)}"
        self.next_policy_id += 1
        end_ts = now + POLICY_DAYS * DAY
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
        )
        self.policy_ids.append(pid)
        return pid

    @gl.public.write
    def expire_policy(self, policy_id: str) -> None:
        """Release the reserve held for a policy that ran out unclaimed."""
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
        if policy_id not in self.policies:
            raise gl.vm.UserError(f"{ERR_EXPECTED} unknown policy")
        pol = self.policies[policy_id]
        if pol.status != "ACTIVE" or pol.claimed:
            raise gl.vm.UserError(f"{ERR_EXPECTED} policy not claimable")
        now = self._now()
        if now > pol.end_ts + EXPIRY_GRACE:
            raise gl.vm.UserError(f"{ERR_EXPECTED} policy expired")
        prior = self.claims.get(policy_id, None)
        if prior is not None and now < prior.evaluated_at + EVAL_MIN_GAP:
            raise gl.vm.UserError(f"{ERR_EXPECTED} TIMELOCK evaluated too recently")

        # Observation window: newest observable day is lagged and capped at
        # policy end; the whole span must sit inside the coverage period.
        today = datetime.fromtimestamp(now - now % DAY, tz=timezone.utc)
        obs_end = today - timedelta(days=DATA_LAG_DAYS)
        cap = datetime.fromtimestamp(int(pol.end_ts) - int(pol.end_ts) % DAY, tz=timezone.utc)
        if obs_end > cap:
            obs_end = cap
        span_days = WINDOW_DAYS * N_WINDOWS
        first_day = obs_end - timedelta(days=span_days - 1)
        if int(first_day.timestamp()) < int(pol.start_ts) - int(pol.start_ts) % DAY + DAY:
            raise gl.vm.UserError(f"{ERR_EXPECTED} TIMELOCK observation windows not yet inside coverage")

        plot = self.plots[pol.plot_id]
        lat, lon = int(plot.latitude_e4), int(plot.longitude_e4)
        baseline_tenths = int(plot.baseline_rainfall_mm) * 10
        windows = _window_bounds(obs_end)
        urls = _source_urls(lat, lon, first_day, obs_end)

        def leader_fn():
            obs = [(n, _observe_source(n, u, windows, baseline_tenths)) for n, u in urls]
            return _corroborate(obs)

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False  # leader error: force rotation, never ratify
            mine = leader_fn()
            theirs = leaders_res.calldata
            # EXACT discrete-tier equality. No tolerance band exists.
            if int(theirs["tier"]) != int(mine["tier"]):
                return False
            return int(theirs["tier"]) in (-1, 0, 1, 2, 3)

        res = gl.vm.run_nondet(leader_fn, validator_fn)
        tier = int(res["tier"])
        if tier not in TIER_LABEL:
            raise gl.vm.UserError(f"{ERR_EXTERNAL} invalid tier")

        evals = (int(prior.evaluations) if prior is not None else 0) + 1
        payout = 0
        outcome = TIER_LABEL[tier]
        if tier >= 1:
            payout = int(pol.coverage_amount) * TIER_PAYOUT_BPS[tier] // 10000
        approved = payout > 0
        self.claims[policy_id] = ClaimRecord(
            payout_tier=max(tier, 0),
            outcome=outcome,
            median_deficit=int(res["median_deficit"]),
            source_count=int(res["source_count"]),
            reasoning=str(res["reasoning"])[:400],
            approved_payout=payout,
            vested_amount=0,
            last_release_timestamp=0,
            cooldown_until=now + COOLDOWN if approved else 0,
            evaluated_at=now,
            evaluations=evals,
        )
        if approved:
            pol.claimed = True
            pol.status = "CLAIM_APPROVED"
            self.total_liabilities -= pol.coverage_amount
            self.pool_balance -= payout
            self.committed_payouts += payout
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
