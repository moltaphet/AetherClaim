# v0.1.0
# { "Depends": "py-genlayer:5jycge4q8k23462jtb0b9fyey1s9qz928sz2nbrd9mg4sxqg2qng" }

# AetherClaim - Decentralized Parametric Climate & Agricultural Insurance.
#
# Parametric drought cover on an authenticated plot registry. A claim is
# settled by GenLayer validators independently re-querying three meteorological
# pipelines hosted by three different operators and agreeing on a DISCRETE
# payout tier. There is no LLM in the settlement path: the verdict is a pure
# function of the observed rainfall.
#
# Design constraints (each one closes a specific, previously exploited vector):
#
#   1. No arbitrary coordinates. Policies attach to a plot_id in a registry.
#      The observation URLs are built INSIDE the contract from registry data;
#      no caller-supplied URL or coordinate ever reaches a fetch.
#   2. Independent plot provenance. A plot is not insurable because the admin
#      typed it, and it is not insurable because a manifest in the project's own
#      repository says so either. `verify_plot_provenance` first requires an
#      Ed25519 ATTESTATION from the CADASTRAL AUTHORITY, a public key fixed at
#      deployment that the admin cannot change and does not hold the private half
#      of. The signature covers the authority's record id, the plot id, the
#      SHA-256 `cadastral_hash` (a commitment to coordinates, 12 baselines, crop
#      cycle, coverage cap and GHCN station) and the eligible policyholder. A
#      fabricated, altered or mismatched plot has no such signature and the call
#      reverts with ERR_INVALID_CADASTRAL_ATTESTATION. Only then do validators
#      fetch the commit-pinned manifest and cross-check every field against it.
#   3. Insurable interest. A plot binds to ONE eligible policyholder (the farm
#      operator named in the manifest). Only that address can buy cover on it.
#   4. No duplicate coverage. One live coverage window per plot, and the plot's
#      unresolved coverage can never exceed its registered `max_plot_coverage`.
#   5. Three operators, three hosts. ERA5 (open-meteo.com), NASA POWER
#      (nasa.gov) and NOAA NCEI GHCN-Daily (noaa.gov). The contract refuses to
#      run a plan in which two sources share a hostname OR an operator domain.
#   6. Multi-source aggregation. With all three answering, the settled tier is
#      their MEDIAN and ANY caller may settle. With exactly two answering
#      (degraded mode), the settled tier is the LOWER of the two, and only the
#      POLICYHOLDER may accept it. With fewer than two, QUORUM_FAILURE.
#   7. Sustained deviation. The tier is the MINIMUM across two consecutive
#      observation windows, so a one-window anomaly pays nothing.
#   8. Discrete payouts. Only 0 / 30 / 70 / 100 percent exist.
#   9. Tier-boundary liveness. Validators accept a leader whose deficit is within
#      DEFICIT_NOISE_TOLERANCE of their own and whose tier is NOT HIGHER than
#      their own tier, so two validators straddling a threshold converge on the
#      lower tier instead of rotating leaders forever. As a backstop the
#      policyholder may call `resolve_boundary_dispute`, which settles at the
#      guaranteed lower bound tier(deficit - tolerance).
#  10. Observation revisions. Evaluation opens MATURITY_LAG_DAYS after the final
#      observed day. During the 24h cooldown of a payout, `re_evaluate_revised_claim`
#      re-reads all three sources and moves the tier (up or down) only when a
#      material revision flips it. The unpaid part of the coverage stays reserved
#      until the cooldown ends, so an upward revision is always fundable.
#  11. Micro-baseline floor, LP orphan guard, worthless-policy guard, cooldown +
#      vesting + exposure caps, as in earlier rounds.

import hashlib
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
ERR_INELIGIBLE_POLICYHOLDER = "ERR_INELIGIBLE_POLICYHOLDER"
ERR_DUPLICATE_PLOT_COVERAGE = "ERR_DUPLICATE_PLOT_COVERAGE"
ERR_PROVENANCE_UNVERIFIED = "ERR_PROVENANCE_UNVERIFIED"
ERR_INVALID_CADASTRAL_ATTESTATION = "ERR_INVALID_CADASTRAL_ATTESTATION"

# --- Protocol constants ------------------------------------------------------
DAY = 86400
STAGING_DELAY = 7 * DAY  # plot staging -> activation
EPOCH_SECONDS = 7 * DAY  # exposure accounting epoch
WINDOW_DAYS = 14  # one observation window
N_WINDOWS = 2  # consecutive windows that must both breach
# Stabilisation lag: evaluation opens this many days after the LAST observed day
# ends, so preliminary satellite/reanalysis estimates have settled first.
MATURITY_LAG_DAYS = 10
# Micro-baseline floor: a window expecting less rain than this cannot evidence a
# drought, so its deficit is forced to 0%.
MIN_INSURABLE_BASELINE_MM = 10
POLICY_DAYS = 90
EVAL_MIN_GAP = DAY  # minimum spacing between evaluations of one policy
COOLDOWN = DAY  # approval -> first release
VESTING = 3 * DAY  # linear vesting length
REVISION_MIN_GAP = 3600  # spacing between revisions inside one cooldown
MAX_REVISIONS = 3
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
MAX_PLOT_COVERAGE_LIMIT = MAX_EXPOSURE_CAP
REASONING_MAX = 400
# Validators may differ on median_deficit by this many points. It is also the
# width of the band inside which a tier straddling a threshold is resolved to
# the LOWER tier (see _validators_agree and _conservative_tier).
DEFICIT_NOISE_TOLERANCE = 5
DAYS_PER_MONTH = 30  # monthly baseline -> per-window baseline scaling
SOURCE_COUNT = 3

# Settlement modes recorded on the claim.
MODE_STANDARD = "STANDARD"
MODE_BOUNDARY = "BOUNDARY_CONSERVATIVE"

# The cadastral manifest the validators check plots against. Pinned to a git
# COMMIT, so its bytes cannot change after this contract is deployed, and served
# from a domain that is neither the contract admin's wallet nor a weather host.
REGISTRY_MANIFEST_URL = (
    "https://raw.githubusercontent.com/moltaphet/AetherClaim/3b39beb2f23a02f8074e9de5113996b16b115ac6/registry/cadastral_manifest.json"
)
CADASTRE_SCHEMA = "AETHERCLAIM-CADASTRE-V1"
ATTESTATION_SCHEMA = "AETHERCLAIM-ATTESTATION-V1"
RECORD_ID_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:/-"

# Discrete payout buckets: tier -> payout bps
TIER_PAYOUT_BPS = {0: 0, 1: 3000, 2: 7000, 3: 10000}
# -2 is the only inconclusive code: fewer than MIN_SOURCES answered.
TIER_LABEL = {
    -2: "QUORUM_FAILURE",
    0: "NORMAL",
    1: "MODERATE_DROUGHT",
    2: "SEVERE_DROUGHT",
    3: "CATASTROPHIC_DROUGHT",
}

# Genesis registry, seeded into storage at deployment and active immediately but
# NOT insurable until verify_plot_provenance has succeeded. lat/lon are degrees *
# 10_000. `monthly` is expected rainfall in mm for each calendar month Jan..Dec.
# The cadastral hash is the SHA-256 of the canonical record (see
# _cadastral_digest). `station` is the NOAA GHCN-Daily station used by the
# third, independent weather source.
GENESIS_MAX_PLOT_COVERAGE = 50 * 10**18
GENESIS_PLOTS = [
    ("CV-ALMOND-01", "Central Valley Almond Basin", "ALMOND", 366000, -1198000,
     [50, 45, 35, 15, 8, 2, 0, 0, 4, 15, 25, 45],
     "eb190f10ac1e3fc5d10eb6f73859c0f3e6ce9cb0a4c0f32c51c39b588599946e", 11, 3, "USW00093193"),
    ("IA-CORN-01", "Iowa Corn Belt", "CORN", 420300, -938200,
     [25, 28, 55, 95, 120, 140, 115, 110, 85, 70, 50, 35],
     "d4f49093f402d3c39709a761f3d45d00b6f2f1c313c123b32212d07c1dc19599", 4, 10, "USC00130200"),
    ("AND-OLIVE-01", "Andalusia Olive Groves", "OLIVE", 373800, -43700,
     [60, 55, 50, 45, 30, 10, 2, 3, 25, 60, 75, 70],
     "b89ac8d42c4cde8bd29c6a451b3c75f3710b0e412eccd19fe01aa9ecabe18e34", 10, 3, "SP000008410"),
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
    cadastral_hash: str  # SHA-256 commitment to the canonical parcel record
    crop_cycle_start_month: u32  # 1..12
    crop_cycle_end_month: u32  # 1..12, may be < start (wraps the year)
    active: bool
    staged_at: u256
    registered_at: u256  # 0 until activated
    max_plot_coverage: u256  # cap on unresolved coverage for this plot
    eligible_policyholder: Address  # the only address that may insure the plot
    ghcnd_station: str  # NOAA GHCN-Daily station for the third source
    provenance_verified: bool
    provenance_verified_at: u256
    provenance_manifest_sha256: str  # digest of the manifest bytes validators read
    registry_record_id: str  # the cadastral authority's id for this parcel ("" until attested)
    attestation_signature: str  # hex Ed25519 signature the authority issued for this plot
    active_coverage: u256  # unresolved coverage currently on this plot
    last_coverage_end: u256  # end_ts of the latest coverage window on this plot


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
    matures_at: u256  # final observed day end + stabilisation lag


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
    revision_reserve: u256  # coverage still held back for a revision, until finalised
    revisions: u256
    settlement_mode: str  # STANDARD | BOUNDARY_CONSERVATIVE
    finalized: bool  # True once nothing more can move the payout


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


def _conservative_tier(deficit_pct: int) -> int:
    """Guaranteed lower bound: the tier that still holds if every validator's
    reading were up to DEFICIT_NOISE_TOLERANCE points below the certified one."""
    return _tier_for_deficit(max(0, deficit_pct - DEFICIT_NOISE_TOLERANCE))


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


def _valid_hex_address(s) -> bool:
    return (
        isinstance(s, str)
        and len(s) == 42
        and s.startswith("0x")
        and all(ch in "0123456789abcdefABCDEF" for ch in s[2:])
    )


def _valid_station(s) -> bool:
    return (
        isinstance(s, str)
        and len(s) == 11
        and all(ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for ch in s)
    )


def _cadastral_digest(plot_id, lat_e4, lon_e4, monthly, c_start, c_end, max_cov, station) -> str:
    """SHA-256 commitment to the parcel record. The eligible policyholder is
    deliberately NOT inside the digest: operator changes are an eligibility
    event, checked field by field, not a change of the parcel."""
    rec = "|".join(
        [
            CADASTRE_SCHEMA,
            str(plot_id),
            str(int(lat_e4)),
            str(int(lon_e4)),
            ",".join(str(int(v)) for v in monthly),
            str(int(c_start)),
            str(int(c_end)),
            str(int(max_cov)),
            str(station),
        ]
    )
    return hashlib.sha256(rec.encode("utf-8")).hexdigest()


# --- Ed25519 (RFC 8032) signature verification -------------------------------
# Pure Python so the contract needs nothing beyond the stdlib. Verification only:
# the contract never holds a private key. Checked against the RFC 8032 test
# vectors and against the `cryptography` package in tests/test_aether_claim.py.
_ED_P = 2**255 - 19
_ED_Q = 2**252 + 27742317777372353535851937790883648493
_ED_D = -121665 * pow(121666, _ED_P - 2, _ED_P) % _ED_P
_ED_SQRT_M1 = pow(2, (_ED_P - 1) // 4, _ED_P)


def _ed_add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _ED_P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _ED_P
    c = 2 * p[3] * q[3] * _ED_D % _ED_P
    d = 2 * p[2] * q[2] % _ED_P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _ED_P, g * h % _ED_P, f * g % _ED_P, e * h % _ED_P)


def _ed_mul(s: int, p):
    r = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            r = _ed_add(r, p)
        p = _ed_add(p, p)
        s >>= 1
    return r


def _ed_recover_x(y: int, sign: int):
    if y >= _ED_P:
        return None
    x2 = (y * y - 1) * pow(_ED_D * y * y + 1, _ED_P - 2, _ED_P) % _ED_P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_ED_P + 3) // 8, _ED_P)
    if (x * x - x2) % _ED_P != 0:
        x = x * _ED_SQRT_M1 % _ED_P
    if (x * x - x2) % _ED_P != 0:
        return None
    if (x & 1) != sign:
        x = _ED_P - x
    return x


def _ed_decompress(b: bytes):
    if len(b) != 32:
        return None
    y = int.from_bytes(b, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _ed_recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _ED_P)


def _ed25519_verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    if len(pub) != 32 or len(sig) != 64:
        return False
    a_pt = _ed_decompress(pub)
    r_pt = _ed_decompress(sig[:32])
    if a_pt is None or r_pt is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _ED_Q:  # reject malleable / non-canonical S
        return False
    gy = 4 * pow(5, _ED_P - 2, _ED_P) % _ED_P
    gx = _ed_recover_x(gy, 0)
    if gx is None:
        return False
    base = (gx, gy, 1, gx * gy % _ED_P)
    h = int.from_bytes(hashlib.sha512(sig[:32] + pub + msg).digest(), "little") % _ED_Q
    lhs, rhs = _ed_mul(s, base), _ed_add(r_pt, _ed_mul(h, a_pt))
    return (lhs[0] * rhs[2] - rhs[0] * lhs[2]) % _ED_P == 0 and (lhs[1] * rhs[2] - rhs[1] * lhs[2]) % _ED_P == 0


def _hex_bytes(s, n: int):
    """Strict lower/upper-case hex of exactly n bytes, else None."""
    if not isinstance(s, str) or len(s) != 2 * n:
        return None
    if any(ch not in "0123456789abcdefABCDEF" for ch in s):
        return None
    return bytes.fromhex(s)


def _valid_record_id(s) -> bool:
    return isinstance(s, str) and 1 <= len(s) <= 64 and all(ch in RECORD_ID_CHARS for ch in s)


def _attestation_message(record_id: str, plot_id: str, cadastral_hash: str, holder_hex: str) -> bytes:
    """The exact bytes the cadastral authority signs for a plot."""
    return "|".join([ATTESTATION_SCHEMA, record_id, plot_id, cadastral_hash.lower(), holder_hex.lower()]).encode("utf-8")


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


def _source_urls(lat_e4: int, lon_e4: int, first: datetime, last: datetime, station: str):
    """Three sources on three different operators. Order is fixed."""
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
            "nasa-power",
            "https://power.larc.nasa.gov/api/temporal/daily/point"
            f"?parameters=PRECTOTCORR&community=AG&longitude={lon}&latitude={lat}"
            f"&start={cmp_a}&end={cmp_b}&format=JSON",
        ),
        (
            "noaa-ncei",
            "https://www.ncei.noaa.gov/access/services/data/v1"
            f"?dataset=daily-summaries&dataTypes=PRCP&stations={station}"
            f"&startDate={iso_a}&endDate={iso_b}&format=json&units=metric",
        ),
    ]


def _host_of(url: str) -> str:
    rest = url.split("://", 1)[1] if "://" in url else url
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return host.split("@")[-1].split(":", 1)[0].lower()


def _operator_domain(host: str) -> str:
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def _sources_independent(urls) -> bool:
    """True only when every source is https and the SOURCE_COUNT hostnames AND
    their registrable operator domains are all pairwise distinct. Distinct
    hostnames alone are not enough: archive-api.open-meteo.com and
    api.open-meteo.com are two hostnames of one operator."""
    if len(urls) != SOURCE_COUNT:
        return False
    hosts = []
    for _, u in urls:
        if not u.startswith("https://"):
            return False
        hosts.append(_host_of(u))
    if any(not h or "." not in h for h in hosts):
        return False
    if len(set(hosts)) != SOURCE_COUNT:
        return False
    return len({_operator_domain(h) for h in hosts}) == SOURCE_COUNT


def _daily_series(source: str, body, days):
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
    if source == "noaa-ncei":
        # GHCN-Daily rows: {"DATE": "YYYY-MM-DD", "PRCP": "mm"}. A day with no row
        # is a missing observation, never a dry day.
        lookup = {}
        for row in body:
            if isinstance(row, dict) and "DATE" in row and "PRCP" in row:
                lookup[row["DATE"]] = row["PRCP"]
        for d in days:
            key = d.strftime("%Y-%m-%d")
            v = lookup.get(key)
            if v is None or v == "":
                return None
            f = float(v)
            if f < 0:
                return None
            out[key] = int(round(f * 10))
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


def _canonical_reasoning(tier: int, median: int, count: int, conservative: bool = False) -> str:
    """Built from committed fields only, never from leader text."""
    if tier == -2:
        return f"QUORUM_FAILURE: {count} of {SOURCE_COUNT} sources answered, {MIN_SOURCES} required; retry permitted"
    if conservative:
        return (
            f"{count} sources answered: boundary resolution at guaranteed lower-bound tier {tier} "
            f"({TIER_LABEL[tier]}), certified sustained deficit {median}%"
        )
    if count == SOURCE_COUNT:
        return (
            f"{count} sources answered: median tier {tier} ({TIER_LABEL[tier]}), "
            f"median sustained deficit {median}%"
        )
    return (
        f"{count} sources answered: conservative floor tier {tier} ({TIER_LABEL[tier]}), "
        f"lower sustained deficit {median}%"
    )


def _payload_ok(obj, conservative: bool = False) -> bool:
    """Strict structural + bounds check of a consensus payload. Pure function:
    used by validators on the leader's output and by the contract again before
    anything is written to storage."""
    if not isinstance(obj, dict):
        return False
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
        # A settled tier needs a quorum, and the deficit must sit in its bucket
        # (the lower-bound bucket in boundary-resolution mode).
        if cnt < MIN_SOURCES:
            return False
        want = _conservative_tier(med) if conservative else _tier_for_deficit(med)
        if want != tier:
            return False
    return True


def _validators_agree(theirs, mine, conservative: bool = False) -> bool:
    """Validator acceptance of a leader payload. Both payloads have already
    passed _payload_ok.

    Settlement-relevant fields:
      * source_count is compared EXACTLY (it decides who may settle).
      * an outage (-2) must be an outage for both.
      * median_deficit must be within DEFICIT_NOISE_TOLERANCE.
      * standard mode: the leader's tier must not be HIGHER than this
        validator's own tier. A leader at 49% (tier 1) is accepted by a
        validator at 50% (tier 2), so a threshold straddle converges on the
        lower tier instead of rotating forever. A leader above the validator is
        rejected, so the payout is never pushed past what any validator sees.
        A lower tier is only possible when the validator's own deficit is
        within DEFICIT_NOISE_TOLERANCE above a threshold (see tests).
      * conservative mode: the tier is a pure function of the certified deficit,
        already enforced by _payload_ok, so nothing further is needed.
    """
    if int(theirs["source_count"]) != int(mine["source_count"]):
        return False
    t_their, t_mine = int(theirs["tier"]), int(mine["tier"])
    if (t_their == -2) != (t_mine == -2):
        return False
    if t_their == -2:
        return True
    if abs(int(theirs["median_deficit"]) - int(mine["median_deficit"])) > DEFICIT_NOISE_TOLERANCE:
        return False
    if conservative:
        return True
    return t_their <= t_mine


def _corroborate(observations):
    """Deterministic consensus over per-source (name, (tier, deficit) | None).

    Aggregation is an ORDER STATISTIC, which makes it monotone in availability:
      * fewer than MIN_SOURCES answered: QUORUM_FAILURE (-2), retry permitted.
      * all three answered: the MEDIAN tier. One faulty station in either
        direction cannot move the payout past the majority view.
      * two answered: the LOWER tier. Conservative: LPs never pay above what both
        answering sources support.

    Dropping a source can only lower the settled tier, never raise it, which is
    why only the policyholder may accept a degraded tier. Each source's tier is a
    non-decreasing function of its deficit, so the k-th order statistic of the
    tiers is the tier of the k-th order statistic of the deficits.
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


def _to_conservative(payload):
    """Re-express a settled payload at the guaranteed lower-bound tier."""
    if payload["tier"] < 0:
        return payload
    tier = _conservative_tier(payload["median_deficit"])
    return {
        "tier": tier,
        "median_deficit": payload["median_deficit"],
        "source_count": payload["source_count"],
        "reasoning": _canonical_reasoning(tier, payload["median_deficit"], payload["source_count"], True),
    }


def _check_manifest(plot_id: str, expected: dict) -> dict:
    """Compare the registry manifest with the plot as stored on-chain. Pure
    function of (manifest bytes, expected): leader and validators must produce
    the same dict. `code` names the first field that disagrees."""
    out = {"verified": False, "code": "MANIFEST_UNAVAILABLE", "manifest_sha256": ""}
    try:
        res = gl.nondet.web.get(REGISTRY_MANIFEST_URL)
        if res.status != 200:
            return out
        import json

        raw = bytes(res.body)
        out["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict) or doc.get("schema") != CADASTRE_SCHEMA:
            out["code"] = "MANIFEST_SCHEMA"
            return out
        rec = doc.get("plots", {}).get(plot_id)
        if not isinstance(rec, dict):
            out["code"] = "PLOT_NOT_IN_MANIFEST"
            return out
        checks = [
            ("LATITUDE", rec.get("latitude_e4"), expected["latitude_e4"]),
            ("LONGITUDE", rec.get("longitude_e4"), expected["longitude_e4"]),
            ("BASELINES", rec.get("monthly_baselines"), expected["monthly_baselines"]),
            ("CROP_CYCLE_START", rec.get("crop_cycle_start_month"), expected["crop_cycle_start_month"]),
            ("CROP_CYCLE_END", rec.get("crop_cycle_end_month"), expected["crop_cycle_end_month"]),
            ("MAX_PLOT_COVERAGE", str(rec.get("max_plot_coverage")), str(expected["max_plot_coverage"])),
            ("GHCND_STATION", rec.get("ghcnd_station"), expected["ghcnd_station"]),
            (
                "ELIGIBLE_POLICYHOLDER",
                str(rec.get("eligible_policyholder", "")).lower(),
                expected["eligible_policyholder"],
            ),
        ]
        for name, got, want in checks:
            if got != want:
                out["code"] = f"MISMATCH_{name}"
                return out
        manifest_hash = str(rec.get("cadastral_hash", "")).lower()
        recomputed = _cadastral_digest(
            plot_id,
            rec["latitude_e4"],
            rec["longitude_e4"],
            rec["monthly_baselines"],
            rec["crop_cycle_start_month"],
            rec["crop_cycle_end_month"],
            int(rec["max_plot_coverage"]),
            rec["ghcnd_station"],
        )
        if manifest_hash != expected["cadastral_hash"] or recomputed != manifest_hash:
            out["code"] = "MISMATCH_CADASTRAL_HASH"
            return out
        out["verified"] = True
        out["code"] = "VERIFIED"
        return out
    except Exception:
        out["verified"] = False
        out["code"] = "MANIFEST_UNREADABLE"
        return out


def _provenance_ok(obj) -> bool:
    if not isinstance(obj, dict) or set(obj.keys()) != {"verified", "code", "manifest_sha256"}:
        return False
    if not isinstance(obj["verified"], bool):
        return False
    code, digest = obj["code"], obj["manifest_sha256"]
    if not isinstance(code, str) or len(code) > 40 or not code.replace("_", "").isalnum():
        return False
    if not isinstance(digest, str) or len(digest) not in (0, 64):
        return False
    if obj["verified"] and (code != "VERIFIED" or len(digest) != 64):
        return False
    return True


def _consensus_observation(urls, windows, baselines_tenths, conservative: bool):
    """Run the three-source observation under validator consensus."""

    def leader_fn():
        obs = [(n, _observe_source(n, u, windows, baselines_tenths)) for n, u in urls]
        payload = _corroborate(obs)
        return _to_conservative(payload) if conservative else payload

    def validator_fn(leaders_res: gl.vm.Result) -> bool:
        if not isinstance(leaders_res, gl.vm.Return):
            return False  # leader error: force rotation, never ratify
        theirs = leaders_res.calldata
        if not _payload_ok(theirs, conservative):
            return False
        mine = leader_fn()
        if not _payload_ok(mine, conservative):
            return False
        return _validators_agree(theirs, mine, conservative)

    res = gl.vm.run_nondet(leader_fn, validator_fn)
    if not _payload_ok(res, conservative):
        raise gl.vm.UserError(f"{ERR_EXTERNAL} malformed consensus payload")
    return res


class AetherClaim(gl.contract.Contract):
    admin: Address
    cadastral_authority: str  # hex Ed25519 public key; fixed at deployment, no setter
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

    def __init__(
        self,
        max_epoch_exposure: int = 500 * 10**18,
        genesis_policyholder: str = "",
        cadastral_authority: str = "",
    ):
        if not (MIN_EXPOSURE_CAP <= max_epoch_exposure <= MAX_EXPOSURE_CAP):
            raise gl.vm.UserError(f"{ERR_EXPECTED} exposure cap out of bounds")
        if not _valid_hex_address(genesis_policyholder):
            raise gl.vm.UserError(f"{ERR_EXPECTED} genesis_policyholder must be a 0x-prefixed 20-byte address")
        holder = Address(genesis_policyholder)
        if holder == gl.message.sender_address:
            raise gl.vm.UserError(f"{ERR_EXPECTED} Admin cannot be the eligible policyholder")
        authority = _hex_bytes(cadastral_authority, 32)
        if authority is None or _ed_decompress(authority) is None:
            raise gl.vm.UserError(f"{ERR_EXPECTED} cadastral_authority must be a 32-byte Ed25519 public key (hex)")
        self.admin = gl.message.sender_address
        self.cadastral_authority = cadastral_authority.lower()
        self.total_lp_shares = 0
        self.pool_balance = 0
        self.total_liabilities = 0
        self.committed_payouts = 0
        self.max_epoch_exposure = max_epoch_exposure
        self.next_policy_id = 1
        now = self._now()
        for pid, name, crop, lat, lon, monthly, chash, c_start, c_end, station in GENESIS_PLOTS:
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
                max_plot_coverage=GENESIS_MAX_PLOT_COVERAGE,
                eligible_policyholder=holder,
                ghcnd_station=station,
                provenance_verified=False,
                provenance_verified_at=0,
                provenance_manifest_sha256="",
                registry_record_id="",
                attestation_signature="",
                active_coverage=0,
                last_coverage_end=0,
            )
            self.plot_ids.append(pid)
        # Fail closed at deployment: the source plan must be independent.
        g = GENESIS_PLOTS[0]
        probe = datetime.fromtimestamp(now, tz=timezone.utc)
        if not _sources_independent(_source_urls(g[3], g[4], probe, probe, g[9])):
            raise gl.vm.UserError(f"{ERR_EXPECTED} weather sources must be on distinct hosts and operators")

    # ------------------------------------------------------------------ utils
    def _now(self) -> int:
        # Deterministic block clock (patched inside GenVM, warp()-able in tests).
        return int(datetime.now(timezone.utc).timestamp())

    def _require_admin(self) -> None:
        if gl.message.sender_address != self.admin:
            raise gl.vm.UserError(f"{ERR_EXPECTED} only admin")

    def _plot_view(self, pid: str) -> dict:
        p = self.plots[pid]
        now = self._now()
        locked_until = int(p.last_coverage_end)
        return {
            "plot_id": pid,
            "name": p.name,
            "crop_type": p.crop_type,
            # Decimal strings: GenVM calldata cannot encode a float return value.
            "latitude": _fmt_e4(int(p.latitude_e4)),
            "longitude": _fmt_e4(int(p.longitude_e4)),
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
            "max_plot_coverage": str(p.max_plot_coverage),
            "eligible_policyholder": p.eligible_policyholder.as_hex,
            "ghcnd_station": p.ghcnd_station,
            "provenance_verified": bool(p.provenance_verified),
            "provenance_verified_at": int(p.provenance_verified_at),
            "provenance_manifest_sha256": p.provenance_manifest_sha256,
            "registry_record_id": p.registry_record_id,
            "attestation_signature": p.attestation_signature,
            "active_coverage": str(p.active_coverage),
            "last_coverage_end": locked_until,
            "coverage_locked": bool(now <= locked_until),
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

    def _observation_plan(self, pol):
        """Windows, per-window baselines and source URLs for a policy. Refuses to
        proceed unless the three sources are on three different operators."""
        obs_end = datetime.fromtimestamp(int(pol.observation_end), tz=timezone.utc)
        obs_start = datetime.fromtimestamp(int(pol.observation_start), tz=timezone.utc)
        plot = self.plots[pol.plot_id]
        monthly = _parse_baselines(plot.monthly_baselines_csv)
        windows = _window_bounds(obs_end)
        baselines_tenths = [_window_baseline_tenths(monthly, ws, we) for ws, we in windows]
        urls = _source_urls(
            int(plot.latitude_e4), int(plot.longitude_e4), obs_start, obs_end, plot.ghcnd_station
        )
        if not _sources_independent(urls):
            raise gl.vm.UserError(f"{ERR_EXPECTED} weather sources must be on distinct hosts and operators")
        return windows, baselines_tenths, urls

    def _release_plot_coverage(self, pol) -> None:
        plot = self.plots[pol.plot_id]
        cov = int(pol.coverage_amount)
        held = int(plot.active_coverage)
        plot.active_coverage = held - cov if held >= cov else 0

    def _finalize_reserve(self, policy_id: str) -> None:
        c = self.claims[policy_id]
        if c.finalized:
            return
        self.total_liabilities -= c.revision_reserve
        c.revision_reserve = 0
        c.finalized = True

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
            "maturity_lag_days": MATURITY_LAG_DAYS,
            "expiry_grace_days": EXPIRY_GRACE_DAYS,
            "quorum_min_sources": MIN_SOURCES,
            "aggregation": "median of 3 answering sources; lower of 2",
            "exposure_cap_bounds": [str(MIN_EXPOSURE_CAP), str(MAX_EXPOSURE_CAP)],
            "policy_days": POLICY_DAYS,
            "epoch_seconds": EPOCH_SECONDS,
            "tier_payout_bps": {str(k): v for k, v in TIER_PAYOUT_BPS.items()},
            "deficit_noise_tolerance": DEFICIT_NOISE_TOLERANCE,
            "revision_min_gap_seconds": REVISION_MIN_GAP,
            "max_revisions": MAX_REVISIONS,
            "registry_manifest_url": REGISTRY_MANIFEST_URL,
            "cadastre_schema": CADASTRE_SCHEMA,
            "attestation_schema": ATTESTATION_SCHEMA,
            "cadastral_authority": self.cadastral_authority,
        }

    @gl.public.view
    def get_weather_sources(self) -> list:
        """The three sources, their hosts and operator domains, plus whether the
        plan is independent. Built from the genesis plot's geometry."""
        g = GENESIS_PLOTS[0]
        probe = datetime(2026, 1, 1, tzinfo=timezone.utc)
        urls = _source_urls(g[3], g[4], probe, probe, g[9])
        ok = _sources_independent(urls)
        return [
            {"name": n, "host": _host_of(u), "operator_domain": _operator_domain(_host_of(u)), "independent": ok}
            for n, u in urls
        ]

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
        revisable = bool(
            not c.finalized
            and c.approved_payout > 0
            and now < c.cooldown_until
            and c.revisions < MAX_REVISIONS
        )
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
            "revisions": int(c.revisions),
            "revision_reserve": str(c.revision_reserve),
            "settlement_mode": c.settlement_mode,
            "finalized": bool(c.finalized),
            "revisable": revisable,
            "revisable_until": int(c.cooldown_until) if revisable else 0,
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
        """Burn shares for a pro-rata slice of the pool, additionally capped at
        the same fraction of UNRESERVED capital, so reserved capital can only
        leave through payouts or policy expiry, never through the first LP to
        the door."""
        key = gl.message.sender_address.as_hex.lower()
        burn = int(shares_to_burn)
        if burn <= 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} zero shares")
        if burn > self.lp_shares.get(key, 0):
            raise gl.vm.UserError(f"{ERR_EXPECTED} exceeds your shares")
        total = int(self.total_lp_shares)
        pool = int(self.pool_balance)
        # Orphan guard: the last LP cannot leave while policies are live.
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
        max_plot_coverage: int,
        eligible_policyholder: str,
        ghcnd_station: str,
    ) -> None:
        """Stage a plot. Staging does not make it insurable: the record must
        also pass verify_plot_provenance against the independent manifest."""
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
        max_cov = int(max_plot_coverage)
        if not (MIN_COVERAGE <= max_cov <= MAX_PLOT_COVERAGE_LIMIT):
            raise gl.vm.UserError(f"{ERR_EXPECTED} max_plot_coverage out of bounds")
        if not _valid_hex_address(eligible_policyholder):
            raise gl.vm.UserError(f"{ERR_EXPECTED} eligible_policyholder must be a 0x-prefixed 20-byte address")
        holder = Address(eligible_policyholder)
        if holder == self.admin or holder == Address("0x" + "00" * 20):
            raise gl.vm.UserError(f"{ERR_EXPECTED} invalid eligible_policyholder")
        if not _valid_station(ghcnd_station):
            raise gl.vm.UserError(f"{ERR_EXPECTED} ghcnd_station must be 11 upper-case alphanumerics")
        # The stored hash must commit to exactly this record.
        want = _cadastral_digest(
            plot_id, latitude_e4, longitude_e4, baselines, crop_cycle_start_month, crop_cycle_end_month, max_cov, ghcnd_station
        )
        if h != want:
            raise gl.vm.UserError(f"{ERR_EXPECTED} cadastral_hash does not commit to the supplied parcel record")
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
            max_plot_coverage=max_cov,
            eligible_policyholder=holder,
            ghcnd_station=ghcnd_station,
            provenance_verified=False,
            provenance_verified_at=0,
            provenance_manifest_sha256="",
            registry_record_id="",
            attestation_signature="",
            active_coverage=0,
            last_coverage_end=0,
        )
        self.plot_ids.append(plot_id)

    @gl.public.write
    def verify_plot_provenance(self, plot_id: str, registry_record_id: str, attestation_signature: str) -> dict:
        """Permissionless. A plot becomes insurable only when BOTH hold:

        1. The CADASTRAL AUTHORITY (the Ed25519 key fixed at deployment) signed
           this exact plot: its record id, plot id, `cadastral_hash` and eligible
           policyholder. A fabricated, altered or mismatched plot has no such
           signature and the call reverts with ERR_INVALID_CADASTRAL_ATTESTATION
           before anything is fetched or written.
        2. Validators fetch the commit-pinned manifest and it matches the plot
           field by field (coordinates, 12 baselines, crop cycle, coverage cap,
           GHCN station, policyholder, and the SHA-256 of that record). A manifest
           mismatch returns verified=false and writes nothing."""
        if plot_id not in self.plots:
            raise gl.vm.UserError(f"{ERR_EXPECTED} unknown plot")
        p = self.plots[plot_id]
        if p.provenance_verified:
            return {
                "plot_id": plot_id,
                "verified": True,
                "code": "ALREADY_VERIFIED",
                "manifest_sha256": p.provenance_manifest_sha256,
            }
        pub = _hex_bytes(self.cadastral_authority, 32)
        sig = _hex_bytes(attestation_signature, 64)
        if pub is None or sig is None or not _valid_record_id(registry_record_id):
            raise gl.vm.UserError(f"{ERR_EXPECTED} {ERR_INVALID_CADASTRAL_ATTESTATION}: malformed attestation")
        message = _attestation_message(registry_record_id, plot_id, p.cadastral_hash, p.eligible_policyholder.as_hex)
        if not _ed25519_verify(pub, message, sig):
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} {ERR_INVALID_CADASTRAL_ATTESTATION}: the cadastral authority did not sign this plot"
            )
        expected = {
            "latitude_e4": int(p.latitude_e4),
            "longitude_e4": int(p.longitude_e4),
            "monthly_baselines": _parse_baselines(p.monthly_baselines_csv),
            "crop_cycle_start_month": int(p.crop_cycle_start_month),
            "crop_cycle_end_month": int(p.crop_cycle_end_month),
            "max_plot_coverage": int(p.max_plot_coverage),
            "ghcnd_station": p.ghcnd_station,
            "eligible_policyholder": p.eligible_policyholder.as_hex.lower(),
            "cadastral_hash": p.cadastral_hash,
        }

        def leader_fn():
            return _check_manifest(plot_id, expected)

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            theirs = leaders_res.calldata
            if not _provenance_ok(theirs):
                return False
            mine = leader_fn()
            return _provenance_ok(mine) and theirs == mine

        res = gl.vm.run_nondet(leader_fn, validator_fn)
        if not _provenance_ok(res):
            raise gl.vm.UserError(f"{ERR_EXTERNAL} malformed provenance payload")
        if res["verified"]:
            p.provenance_verified = True
            p.provenance_verified_at = self._now()
            p.provenance_manifest_sha256 = res["manifest_sha256"]
            p.registry_record_id = registry_record_id
            p.attestation_signature = attestation_signature.lower()
        return {
            "plot_id": plot_id,
            "verified": bool(res["verified"]),
            "code": res["code"],
            "manifest_sha256": res["manifest_sha256"],
        }

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
        sender = gl.message.sender_address
        if sender == self.admin:
            raise gl.vm.UserError(f"{ERR_EXPECTED} Admin cannot hold insurance policies")
        if plot_id not in self.plots:
            raise gl.vm.UserError(f"{ERR_EXPECTED} plot not in authenticated registry")
        plot_rec = self.plots[plot_id]
        if not plot_rec.active:
            raise gl.vm.UserError(f"{ERR_EXPECTED} plot not active")
        if not plot_rec.provenance_verified:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} {ERR_PROVENANCE_UNVERIFIED}: plot has no valid cadastral-authority attestation and manifest match"
            )
        # Insurable interest: only the registry-bound farm operator may insure it.
        if sender != plot_rec.eligible_policyholder:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} {ERR_INELIGIBLE_POLICYHOLDER}: caller is not the registered operator of this plot"
            )
        if coverage < MIN_COVERAGE:
            raise gl.vm.UserError(f"{ERR_EXPECTED} coverage below minimum")
        t_now = self._now()
        # Seasonality: every calendar month the coverage term touches must be
        # inside the plot's registered crop cycle.
        c_start, c_end = int(plot_rec.crop_cycle_start_month), int(plot_rec.crop_cycle_end_month)
        probes = [t_now + d * DAY for d in range(0, POLICY_DAYS + 1, 7)] + [t_now + POLICY_DAYS * DAY]
        for ts in probes:
            month = datetime.fromtimestamp(ts, tz=timezone.utc).month
            if not _month_in_cycle(month, c_start, c_end):
                raise gl.vm.UserError(f"{ERR_EXPECTED} coverage term extends outside the crop cycle (off-season)")
        # Duplicate coverage: one live window per plot, and unresolved coverage on
        # the plot may never exceed its registered cap.
        if t_now <= int(plot_rec.last_coverage_end):
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} {ERR_DUPLICATE_PLOT_COVERAGE}: plot already has cover running until "
                f"{int(plot_rec.last_coverage_end)}"
            )
        if int(plot_rec.active_coverage) + coverage > int(plot_rec.max_plot_coverage):
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} {ERR_DUPLICATE_PLOT_COVERAGE}: plot coverage cap "
                f"{int(plot_rec.max_plot_coverage)} would be exceeded"
            )
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
        now = t_now
        epoch = now // EPOCH_SECONDS
        ekey = str(epoch)
        end_ts = now + POLICY_DAYS * DAY
        # Observation windows are the FINAL 28 days of the term, fixed here at
        # purchase. observation_end is the last full day inside the term.
        obs_end_ts = end_ts - end_ts % DAY - DAY
        obs_start_ts = obs_end_ts - (WINDOW_DAYS * N_WINDOWS - 1) * DAY
        # The verdict opens MATURITY_LAG_DAYS after that last day has ended.
        matures_ts = obs_end_ts + DAY + MATURITY_LAG_DAYS * DAY
        # Worthless-policy guard.
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
        plot_rec.active_coverage = int(plot_rec.active_coverage) + coverage
        plot_rec.last_coverage_end = end_ts

        pid = f"P-{int(self.next_policy_id)}"
        self.next_policy_id += 1
        self.policies[pid] = PolicyRecord(
            policyholder=sender,
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
        """Release the reserve of an unclaimed policy once the grace period ends."""
        if policy_id not in self.policies:
            raise gl.vm.UserError(f"{ERR_EXPECTED} unknown policy")
        pol = self.policies[policy_id]
        if pol.status != "ACTIVE":
            raise gl.vm.UserError(f"{ERR_EXPECTED} policy not active")
        if self._now() <= pol.end_ts + EXPIRY_GRACE:
            raise gl.vm.UserError(f"{ERR_EXPECTED} TIMELOCK still inside claim grace period")
        pol.status = "EXPIRED"
        self.total_liabilities -= pol.coverage_amount
        self._release_plot_coverage(pol)

    # ----------------------------------------------------------------- claims
    def _settle_flow(self, policy_id: str, mode: str) -> dict:
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
        is_holder = gl.message.sender_address == pol.policyholder
        conservative = mode == MODE_BOUNDARY
        if conservative and not is_holder:
            raise gl.vm.UserError(f"{ERR_EXPECTED} only the policyholder may invoke boundary resolution")

        windows, baselines_tenths, urls = self._observation_plan(pol)
        res = _consensus_observation(urls, windows, baselines_tenths, conservative)
        tier = int(res["tier"])
        count = int(res["source_count"])

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
        coverage = int(pol.coverage_amount)
        payout = 0 if tier <= 0 else coverage * TIER_PAYOUT_BPS[tier] // 10000
        med = int(res["median_deficit"])
        settled = tier != -2
        self.claims[policy_id] = ClaimRecord(
            payout_tier=max(tier, 0),
            outcome=TIER_LABEL[tier],
            median_deficit=med,
            source_count=count,
            reasoning=_canonical_reasoning(tier, med, count, conservative),
            approved_payout=payout,
            vested_amount=0,
            last_release_timestamp=0,
            cooldown_until=now + COOLDOWN if payout > 0 else 0,
            evaluated_at=now,
            evaluations=evals,
            # The unpaid part of the coverage stays reserved through the cooldown
            # so a material upward revision is always fundable.
            revision_reserve=(coverage - payout) if payout > 0 else 0,
            revisions=0,
            settlement_mode=mode,
            finalized=(not settled) or payout == 0,
        )
        if not settled:
            # Quorum failure: an outage, not a verdict. Stay ACTIVE for a retry.
            return self.get_claim(policy_id)
        self._release_plot_coverage(pol)
        if payout > 0:
            pol.claimed = True
            pol.status = "SETTLED_PAYOUT"
            # Only the payout leaves the reserve now; coverage - payout is held
            # until the cooldown ends (finalize_claim / release_payout).
            self.total_liabilities -= payout
            self.pool_balance -= payout
            self.committed_payouts += payout
        else:
            self.total_liabilities -= coverage
            pol.status = "SETTLED_NO_PAYOUT"
        return self.get_claim(policy_id)

    @gl.public.write
    def evaluate_claim(self, policy_id: str) -> dict:
        """Settle a policy's claim against its fixed observation windows.

        Timing and permission rules:
          * Windows are fixed at purchase. Evaluation opens MATURITY_LAG_DAYS
            after the last observed day and closes at end_ts + EXPIRY_GRACE.
          * With all three sources answering, any caller may settle, at the
            median. With exactly two answering, only the policyholder may settle,
            at the lower tier; for anyone else the result is QUORUM_FAILURE and NO
            state is written.
          * Only QUORUM_FAILURE leaves a policy ACTIVE. Every settled outcome is
            final on first settlement, except the cooldown revision window
            (re_evaluate_revised_claim).
          * If validators persistently straddle a tier threshold, a leader at the
            lower tier is accepted (see _validators_agree), and the policyholder
            can always fall back to resolve_boundary_dispute.
        """
        return self._settle_flow(policy_id, MODE_STANDARD)

    @gl.public.write
    def resolve_boundary_dispute(self, policy_id: str) -> dict:
        """Policyholder-only fallback for a claim stuck at a tier threshold.

        Settles at the guaranteed lower bound: the tier of (certified deficit -
        DEFICIT_NOISE_TOLERANCE). Every validator that accepts it saw a tier at
        or above the paid tier, so no validator view is ever contradicted
        upward, and consensus does not depend on which side of the threshold a
        validator sits. It is policyholder-only because it can pay LESS than
        evaluate_claim (even nothing): it is a consented floor, not a default."""
        return self._settle_flow(policy_id, MODE_BOUNDARY)

    @gl.public.write
    def re_evaluate_revised_claim(self, policy_id: str) -> dict:
        """Re-read all three sources during the payout cooldown and move the tier
        if upstream stations revised the observations materially.

        Permissionless, because it can move money either way and needs all
        three sources. A revision applies only when the tier changes AND the
        deficit moved by more than DEFICIT_NOISE_TOLERANCE, so ordinary noise at
        a threshold cannot flip a payout back and forth. The cooldown is not
        extended: once streaming begins the settlement is final."""
        if policy_id not in self.policies or policy_id not in self.claims:
            raise gl.vm.UserError(f"{ERR_EXPECTED} no settled claim")
        pol = self.policies[policy_id]
        c = self.claims[policy_id]
        now = self._now()
        if pol.status != "SETTLED_PAYOUT" or c.finalized or c.approved_payout == 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} claim is not revisable")
        if now >= int(c.cooldown_until):
            raise gl.vm.UserError(f"{ERR_EXPECTED} TIMELOCK cooldown elapsed: streaming has begun, settlement is final")
        if int(c.revisions) >= MAX_REVISIONS:
            raise gl.vm.UserError(f"{ERR_EXPECTED} revision limit reached")
        if now < int(c.evaluated_at) + REVISION_MIN_GAP:
            raise gl.vm.UserError(f"{ERR_EXPECTED} TIMELOCK revised too recently")

        windows, baselines_tenths, urls = self._observation_plan(pol)
        res = _consensus_observation(urls, windows, baselines_tenths, False)
        new_tier = int(res["tier"])
        new_med = int(res["median_deficit"])
        old_tier = int(c.payout_tier)
        material = (
            new_tier >= 0
            and int(res["source_count"]) == SOURCE_COUNT
            and new_tier != old_tier
            and abs(new_med - int(c.median_deficit)) > DEFICIT_NOISE_TOLERANCE
        )
        if not material:
            view = self.get_claim(policy_id)
            view["revised"] = False
            return view

        coverage = int(pol.coverage_amount)
        old_payout = int(c.approved_payout)
        new_payout = 0 if new_tier == 0 else coverage * TIER_PAYOUT_BPS[new_tier] // 10000
        if new_payout > old_payout:
            extra = new_payout - old_payout
            # Fundable by construction: the coverage was reserved, and
            # revision_reserve == coverage - old_payout >= extra.
            self.pool_balance -= extra
            self.committed_payouts += extra
            self.total_liabilities -= extra
            c.revision_reserve = int(c.revision_reserve) - extra
        elif new_payout < old_payout:
            back = old_payout - new_payout
            self.pool_balance += back
            self.committed_payouts -= back
            self.total_liabilities += back
            c.revision_reserve = int(c.revision_reserve) + back
        c.approved_payout = new_payout
        c.payout_tier = new_tier
        c.outcome = TIER_LABEL[new_tier]
        c.median_deficit = new_med
        c.source_count = int(res["source_count"])
        c.reasoning = _canonical_reasoning(new_tier, new_med, int(res["source_count"]))
        c.evaluated_at = now
        c.evaluations = int(c.evaluations) + 1
        c.revisions = int(c.revisions) + 1
        if new_payout == 0:
            # Revised down to nothing: the policy closes without a payout.
            self._finalize_reserve(policy_id)
            c.cooldown_until = 0
            pol.claimed = False
            pol.status = "SETTLED_NO_PAYOUT"
        view = self.get_claim(policy_id)
        view["revised"] = True
        return view

    @gl.public.write
    def finalize_claim(self, policy_id: str) -> None:
        """Permissionless. After the cooldown, release the reserve that was held
        back for revisions so LP capital is never held hostage."""
        if policy_id not in self.claims:
            raise gl.vm.UserError(f"{ERR_EXPECTED} no claim")
        c = self.claims[policy_id]
        if c.finalized:
            raise gl.vm.UserError(f"{ERR_EXPECTED} already finalized")
        if self._now() < int(c.cooldown_until):
            raise gl.vm.UserError(f"{ERR_EXPECTED} TIMELOCK revision window still open")
        self._finalize_reserve(policy_id)

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
        self._finalize_reserve(policy_id)
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
