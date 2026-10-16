// Pure, dependency-free mirrors of AetherClaim's underwriting and settlement
// rules, so the UI explains exactly what the contract will enforce. Nothing here
// replaces the contract: every rule is re-checked on-chain.

export const DEFICIT_NOISE_TOLERANCE = 5; // contract: DEFICIT_NOISE_TOLERANCE
export const MATURITY_LAG_DAYS = 10; // contract: MATURITY_LAG_DAYS
export const REVISION_MIN_GAP_SECONDS = 3600; // contract: REVISION_MIN_GAP
export const MAX_REVISIONS = 3; // contract: MAX_REVISIONS
export const TIER_THRESHOLDS = [25, 50, 75] as const;

export function tierForDeficit(deficitPct: number): number {
  if (deficitPct >= 75) return 3;
  if (deficitPct >= 50) return 2;
  if (deficitPct >= 25) return 1;
  return 0;
}

/** Guaranteed lower bound: the tier that still holds if readings were up to the noise tolerance lower. */
export function conservativeTier(deficitPct: number): number {
  return tierForDeficit(Math.max(0, deficitPct - DEFICIT_NOISE_TOLERANCE));
}

/**
 * Validators within the noise tolerance of each other can land on opposite
 * sides of a threshold when it falls inside (deficit - tol, deficit + tol].
 * Returns that threshold, or null.
 */
export function straddledThreshold(deficitPct: number): number | null {
  for (const t of TIER_THRESHOLDS) {
    if (deficitPct - DEFICIT_NOISE_TOLERANCE < t && t <= deficitPct + DEFICIT_NOISE_TOLERANCE) return t;
  }
  return null;
}

/** Mirror of `_validators_agree` (standard mode) for one validator's view. */
export function validatorAccepts(
  leader: { tier: number; deficit: number; sources: number },
  mine: { tier: number; deficit: number; sources: number },
): boolean {
  if (leader.sources !== mine.sources) return false;
  if ((leader.tier === -2) !== (mine.tier === -2)) return false;
  if (leader.tier === -2) return true;
  if (Math.abs(leader.deficit - mine.deficit) > DEFICIT_NOISE_TOLERANCE) return false;
  return leader.tier <= mine.tier;
}

// ---- plot provenance, eligibility, duplicate coverage -----------------------------
export interface UnderwritingPlot {
  active?: boolean;
  provenance_verified?: boolean;
  eligible_policyholder?: string;
  max_plot_coverage?: string;
  active_coverage?: string;
  last_coverage_end?: number;
}

export type Eligibility = "eligible" | "ineligible" | "unknown";

export function eligibility(plot: UnderwritingPlot | undefined, account: string | null): Eligibility {
  if (!plot || !plot.eligible_policyholder || !account) return "unknown";
  return plot.eligible_policyholder.toLowerCase() === account.toLowerCase() ? "eligible" : "ineligible";
}

export function coverageLock(plot: UnderwritingPlot | undefined, nowSec: number) {
  const until = plot?.last_coverage_end ?? 0;
  const max = BigInt(plot?.max_plot_coverage ?? "0");
  const used = BigInt(plot?.active_coverage ?? "0");
  return {
    locked: until > 0 && nowSec <= until,
    until,
    max,
    used,
    headroom: max > used ? max - used : 0n,
  };
}

export type Blocker =
  | "ERR_PROVENANCE_UNVERIFIED"
  | "ERR_INELIGIBLE_POLICYHOLDER"
  | "ERR_DUPLICATE_PLOT_COVERAGE"
  | "PLOT_NOT_ACTIVE";

/** Mirror of the contract's `create_policy` gate order for underwriting checks. */
export function buyBlockers(
  plot: UnderwritingPlot | undefined,
  account: string | null,
  nowSec: number,
  coverageAtto: bigint,
): Blocker[] {
  const out: Blocker[] = [];
  if (!plot) return out;
  if (plot.active === false) out.push("PLOT_NOT_ACTIVE");
  if (plot.provenance_verified === false) out.push("ERR_PROVENANCE_UNVERIFIED");
  if (eligibility(plot, account) === "ineligible") out.push("ERR_INELIGIBLE_POLICYHOLDER");
  const lock = coverageLock(plot, nowSec);
  if (lock.locked || (coverageAtto > 0n && lock.max > 0n && lock.used + coverageAtto > lock.max)) {
    out.push("ERR_DUPLICATE_PLOT_COVERAGE");
  }
  return out;
}

export const BLOCKER_TEXT: Record<Blocker, string> = {
  PLOT_NOT_ACTIVE: "This plot is staged and not yet active.",
  ERR_PROVENANCE_UNVERIFIED:
    "Plot provenance is not verified. No cadastral-authority attestation has been verified on-chain for this plot, so it cannot be insured.",
  ERR_INELIGIBLE_POLICYHOLDER:
    "This wallet is not the registered operator of this plot. Only the farm operator named in the cadastral manifest can insure it.",
  ERR_DUPLICATE_PLOT_COVERAGE:
    "This plot already has cover running, or the amount would exceed its registered coverage cap. One live coverage window per plot.",
};

// ---- weather source independence ---------------------------------------------------
export interface WeatherSource {
  key: string;
  label: string;
  operator: string;
  host: string;
  role: string;
}

export const WEATHER_SOURCES: WeatherSource[] = [
  { key: "era5", label: "ERA5 reanalysis", operator: "Open-Meteo / ECMWF", host: "archive-api.open-meteo.com", role: "gridded reanalysis" },
  { key: "nasa", label: "NASA POWER", operator: "NASA Langley", host: "power.larc.nasa.gov", role: "satellite + model agroclimatology" },
  { key: "ncei", label: "NOAA NCEI GHCN-Daily", operator: "NOAA", host: "www.ncei.noaa.gov", role: "ground rain-gauge station" },
];

export function hostOf(url: string): string {
  const rest = url.includes("://") ? url.split("://", 2)[1] : url;
  const host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0];
  return host.split("@").pop()!.split(":", 1)[0].toLowerCase();
}

export function operatorDomain(host: string): string {
  const labels = host.split(".");
  return labels.length >= 2 ? labels.slice(-2).join(".") : host;
}

/** Mirror of `_sources_independent`: https, 3 distinct hostnames AND 3 distinct operator domains. */
export function sourcesIndependent(urls: string[]): boolean {
  if (urls.length !== 3) return false;
  if (!urls.every((u) => u.startsWith("https://"))) return false;
  const hosts = urls.map(hostOf);
  if (hosts.some((h) => !h || !h.includes("."))) return false;
  if (new Set(hosts).size !== 3) return false;
  return new Set(hosts.map(operatorDomain)).size === 3;
}

// ---- revisions -------------------------------------------------------------------
export interface ClaimLike {
  approved_payout: string;
  cooldown_until: number;
  evaluated_at: number;
  revisions?: number;
  finalized?: boolean;
}

export type RevisionState = "none" | "wait" | "open" | "limit" | "closed";

/** Whether re_evaluate_revised_claim can be sent now, and if not, why. */
export function revisionState(claim: ClaimLike | null | undefined, nowSec: number): RevisionState {
  if (!claim || BigInt(claim.approved_payout) === 0n || claim.finalized) return "none";
  if (nowSec >= claim.cooldown_until) return "closed";
  if ((claim.revisions ?? 0) >= MAX_REVISIONS) return "limit";
  if (nowSec < claim.evaluated_at + REVISION_MIN_GAP_SECONDS) return "wait";
  return "open";
}

/** finalize_claim releases the revision reserve once the cooldown has passed. */
export function canFinalize(claim: ClaimLike | null | undefined, nowSec: number): boolean {
  return !!claim && !claim.finalized && BigInt(claim.approved_payout) > 0n && nowSec >= claim.cooldown_until;
}
