// AetherClaim contract bindings: dual-RPC reads with timeout guards, and
// wallet writes with explicit gas / fee overrides.

export const CHAIN_ID = 61997;
export const CHAIN_ID_HEX = "0xf22d";
export const CONTRACT_ADDRESS =
  process.env.NEXT_PUBLIC_AETHER_CLAIM_ADDRESS ?? "0xfCFC35A4eedc8258e72c65877F5d66bc5Ac3C419";

// Primary first, fallback second. Both serve chain 61997.
export const RPC_URLS = [
  "https://studio-next.genlayer.com/api",
  "https://studio-dev.genlayer.com/api",
];
export const EXPLORER = "https://explorer-studio-next.genlayer.com";

export const READ_TIMEOUT_MS = 5000; // hard ceiling per read, across both RPCs

export const ATTO = 10n ** 18n;
export const PREMIUM_BPS = 800n;
export const TIERS = [
  { tier: 0, label: "NORMAL", min: 0, payoutBps: 0, color: "text-sprout" },
  { tier: 1, label: "MODERATE_DROUGHT", min: 25, payoutBps: 3000, color: "text-wheat" },
  { tier: 2, label: "SEVERE_DROUGHT", min: 50, payoutBps: 7000, color: "text-orange-400" },
  { tier: 3, label: "CATASTROPHIC_DROUGHT", min: 75, payoutBps: 10000, color: "text-rust" },
] as const;

export function tierForDeficit(deficitPct: number): number {
  if (deficitPct >= 75) return 3;
  if (deficitPct >= 50) return 2;
  if (deficitPct >= 25) return 1;
  return 0;
}

export function toAtto(gen: string): bigint {
  const [w, f = ""] = gen.trim().split(".");
  const frac = (f + "0".repeat(18)).slice(0, 18);
  try {
    return BigInt(w || "0") * ATTO + BigInt(frac || "0");
  } catch {
    return 0n;
  }
}

export function fromAtto(v: string | bigint | number, dp = 4): string {
  const n = BigInt(v);
  const whole = n / ATTO;
  const frac = (n % ATTO).toString().padStart(18, "0").slice(0, dp);
  return `${whole}.${frac}`;
}

// ---- value normalisation: genlayer-js returns Map / bigint --------------------
export function normalise(v: unknown): unknown {
  if (v instanceof Map) {
    const o: Record<string, unknown> = {};
    v.forEach((val, k) => (o[String(k)] = normalise(val)));
    return o;
  }
  if (Array.isArray(v)) return v.map(normalise);
  if (typeof v === "bigint") return Number.isSafeInteger(Number(v)) ? Number(v) : v.toString();
  if (v && typeof v === "object") {
    const o: Record<string, unknown> = {};
    for (const [k, val] of Object.entries(v)) o[k] = normalise(val);
    return o;
  }
  return v;
}

function withTimeout<T>(p: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const t = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
    p.then(
      (v) => {
        clearTimeout(t);
        resolve(v);
      },
      (e) => {
        clearTimeout(t);
        reject(e);
      },
    );
  });
}

type AnyClient = Record<string, (...a: any[]) => Promise<any>>;

async function sdk() {
  return (await import("genlayer-js")) as unknown as {
    createClient: (cfg: unknown) => AnyClient;
    chains: Record<string, { rpcUrls: unknown } & Record<string, unknown>>;
  };
}

async function buildClient(rpc: string, account?: string, provider?: unknown): Promise<AnyClient> {
  const s = await sdk();
  const base = s.chains.studioDevnet;
  const chain = { ...base, rpcUrls: { default: { http: [rpc] } } };
  return s.createClient(account ? { chain, account, provider } : { chain });
}

const readClients = new Map<string, AnyClient>();

// ---- read-health store (drives the "syncing" indicator) ------------------------
export type ReadHealth = "unknown" | "live" | "cached";
let health: ReadHealth = "unknown";
const listeners = new Set<() => void>();
export const readHealth = {
  get: () => health,
  subscribe: (fn: () => void) => {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
};
function setHealth(h: ReadHealth) {
  if (h !== health) {
    health = h;
    listeners.forEach((l) => l());
  }
}

async function readOne<T>(rpc: string, functionName: string, args: unknown[]): Promise<T> {
  let client = readClients.get(rpc);
  if (!client) {
    client = await buildClient(rpc);
    readClients.set(rpc, client);
  }
  const out = await client.readContract({ address: CONTRACT_ADDRESS, functionName, args });
  return normalise(out) as T;
}

/**
 * Query both RPCs in parallel; first success wins. The whole read is bounded by
 * READ_TIMEOUT_MS so callers can never hang.
 */
export async function readView<T = unknown>(functionName: string, args: unknown[] = []): Promise<T> {
  const attempts = RPC_URLS.map((rpc) => readOne<T>(rpc, functionName, args));
  return withTimeout(Promise.any(attempts), READ_TIMEOUT_MS, `${functionName}`).catch((e) => {
    throw e instanceof AggregateError ? new Error(String(e.errors[0]?.message ?? e.errors[0])) : e;
  });
}

/** Read with a guaranteed answer: on failure/timeout, return `fallback` and flag cached state. */
export async function readOr<T>(functionName: string, args: unknown[], fallback: T): Promise<{ data: T; live: boolean }> {
  try {
    const data = await readView<T>(functionName, args);
    setHealth("live");
    return { data, live: true };
  } catch {
    setHealth("cached");
    return { data: fallback, live: false };
  }
}

// ---- wallet --------------------------------------------------------------------
export type Eip1193 = {
  request: (a: { method: string; params?: unknown[] }) => Promise<unknown>;
  on?: (ev: string, fn: (...a: any[]) => void) => void;
  removeListener?: (ev: string, fn: (...a: any[]) => void) => void;
};

export function injected(): Eip1193 | undefined {
  if (typeof window === "undefined") return undefined;
  return (window as unknown as { ethereum?: Eip1193 }).ethereum;
}

/**
 * Wraps the injected provider so each eth_sendTransaction envelope carries a gas
 * limit derived by the GenLayer SDK (`estimateTransactionGas`) for that exact
 * transaction. Nothing is hardcoded: if the SDK cannot estimate, the field is
 * left unset and the wallet sizes it itself.
 */
export function withSdkGasEstimation(p: Eip1193, getClient: () => AnyClient | undefined): Eip1193 {
  return {
    ...p,
    request: async (a) => {
      if (a.method === "eth_sendTransaction" && a.params?.[0]) {
        const tx = { ...(a.params[0] as Record<string, any>) };
        const client = getClient();
        if (client?.estimateTransactionGas && tx.gas === undefined) {
          try {
            const gas = (await client.estimateTransactionGas({
              from: tx.from,
              to: tx.to,
              data: tx.data,
              value: tx.value ? BigInt(tx.value) : undefined,
            })) as bigint;
            tx.gas = "0x" + gas.toString(16);
          } catch {
            /* leave to the wallet */
          }
        }
        return p.request({ ...a, params: [tx, ...a.params.slice(1)] });
      }
      return p.request(a);
    },
  };
}

export async function ensureChain(p: Eip1193): Promise<void> {
  const current = (await p.request({ method: "eth_chainId" })) as string;
  if (current?.toLowerCase() === CHAIN_ID_HEX) return;
  try {
    await p.request({ method: "wallet_switchEthereumChain", params: [{ chainId: CHAIN_ID_HEX }] });
  } catch (e: any) {
    if (e?.code === 4902 || e?.code === -32603) {
      await p.request({
        method: "wallet_addEthereumChain",
        params: [
          {
            chainId: CHAIN_ID_HEX,
            chainName: "GenLayer Studio Next",
            nativeCurrency: { name: "GEN", symbol: "GEN", decimals: 18 },
            rpcUrls: RPC_URLS,
            blockExplorerUrls: [EXPLORER],
          },
        ],
      });
    } else {
      throw e;
    }
  }
}

export interface WriteResult {
  hash: string;
  status: string;
}

export async function writeCall(
  account: string,
  functionName: string,
  args: unknown[] = [],
  value: bigint = 0n,
): Promise<WriteResult> {
  const p = injected();
  if (!p) throw new Error("No wallet detected");
  await ensureChain(p);

  // Injected EIP-1193 provider -> official GenLayer client. The wrapper fills
  // `gas` from the SDK's own estimate; the client is looked up lazily because
  // it is constructed with the wrapped provider.
  let client: AnyClient | undefined;
  client = await buildClient(RPC_URLS[0], account, withSdkGasEstimation(p, () => client));

  // Protocol fee deposit: derived from the live fee policy and a write
  // simulation of this exact call. If the simulation path is unavailable, use
  // the policy-only estimate; never dispatch with an implied zero deposit.
  const target = { address: CONTRACT_ADDRESS, functionName, args, value };
  let estimate: { distribution: unknown; messageAllocations?: unknown; feeValue: bigint };
  try {
    estimate = await withTimeout(client.estimateTransactionFeesForWrite(target), 15000, "estimateTransactionFeesForWrite");
  } catch {
    estimate = await withTimeout(client.estimateTransactionFees({}), READ_TIMEOUT_MS, "estimateTransactionFees");
  }
  const fees = {
    distribution: estimate.distribution,
    messageAllocations: estimate.messageAllocations,
    feeValue: estimate.feeValue,
  };

  const hash = (await client.writeContract({ ...target, fees })) as string;
  const receipt = (await client.waitForTransactionReceipt({
    hash,
    waitUntil: "decided",
    retries: 120,
  })) as { statusName?: string; status?: string | number };
  return { hash, status: String(receipt?.statusName ?? receipt?.status ?? "SUBMITTED") };
}

// ---- typed views -------------------------------------------------------------
export interface Plot {
  plot_id: string;
  name: string;
  crop_type: string;
  latitude: number;
  longitude: number;
  latitude_e4: number;
  longitude_e4: number;
  monthly_baselines: number[]; // expected mm, Jan..Dec
  cadastral_hash: string;
  crop_cycle: string;
  crop_cycle_start_month: number;
  crop_cycle_end_month: number;
  active: boolean;
  registered_at: number;
}
export interface Policy {
  policy_id: string;
  policyholder: string;
  plot_id: string;
  coverage_amount: string;
  premium_paid: string;
  start_ts: number;
  end_ts: number;
  claimed: boolean;
  status: string;
  observation_start: number; // unix seconds, fixed at purchase
  observation_end: number;
  matures_at: number; // evaluation opens here (term end + reanalysis lag)
}
export interface Claim {
  policy_id: string;
  payout_tier: number;
  outcome: string;
  median_deficit: number;
  source_count: number;
  reasoning: string;
  approved_payout: string;
  vested_amount: string;
  releasable: string;
  cooldown_until: number;
  vesting_end: number;
  evaluated_at: number;
}
export interface Pool {
  pool_balance: string;
  total_liabilities: string;
  committed_payouts: string;
  free_capital: string;
  epoch: number;
  epoch_exposure: string;
  max_epoch_exposure: string;
  total_lp_shares: string;
  share_price_atto: string;
}

// Pre-seeded genesis state (mirrors GENESIS_PLOTS in contracts/aether_claim.py).
export const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const DAYS_PER_MONTH = 30;
export const WINDOW_DAYS = 14;
export const STAGING_DELAY_DAYS = 7;
export const MIN_INSURABLE_BASELINE_MM = 10; // windows expecting less rain cannot pay
export const DATA_LAG_DAYS = 6;
export const TERM_DAYS = 90;
export const EXPIRY_GRACE_DAYS = 30; // evaluation stays open this long after term end

export function monthInCycle(month: number, start: number, end: number): boolean {
  return start <= end ? month >= start && month <= end : month >= start || month <= end;
}

/** Mirror of the contract's coverage check: every month the 90-day term touches must be in-cycle. */
export function termInsideCycle(p: Pick<Plot, "crop_cycle_start_month" | "crop_cycle_end_month">, from = new Date()): boolean {
  for (let d = 0; d <= 90; d += 7) {
    const m = new Date(from.getTime() + d * 86400000).getUTCMonth() + 1;
    if (!monthInCycle(m, p.crop_cycle_start_month, p.crop_cycle_end_month)) return false;
  }
  const m = new Date(from.getTime() + 90 * 86400000).getUTCMonth() + 1;
  return monthInCycle(m, p.crop_cycle_start_month, p.crop_cycle_end_month);
}

/** A window can pay only if its expected rain clears the micro-baseline floor. */
export function windowInsurable(p: Pick<Plot, "monthly_baselines">, month: number): boolean {
  return windowBaselineMm(p, month) >= MIN_INSURABLE_BASELINE_MM;
}

/**
 * Both final observation windows of a term bought at `from` (default now) must
 * clear the floor, otherwise the policy can never pay. Mirrors the contract:
 * observation_end is the last full day inside the term, and window midpoints
 * sit 6.5 and 20.5 days before it.
 */
export function termWindowsInsurable(p: Pick<Plot, "monthly_baselines">, from = new Date()): boolean {
  const DAY = 86400000;
  const endDay = Date.UTC(from.getUTCFullYear(), from.getUTCMonth(), from.getUTCDate()) + TERM_DAYS * DAY;
  const obsEnd = endDay - DAY;
  const month = (ms: number) => new Date(ms).getUTCMonth() + 1;
  return windowInsurable(p, month(obsEnd - 20.5 * DAY)) && windowInsurable(p, month(obsEnd - 6.5 * DAY));
}

/** Expected rain (mm) for one 14-day window whose midpoint falls in `month` (1..12). */
export function windowBaselineMm(p: Pick<Plot, "monthly_baselines">, month: number): number {
  return Math.floor((p.monthly_baselines[month - 1] * 10 * WINDOW_DAYS) / DAYS_PER_MONTH) / 10;
}

// Pre-seeded genesis state (mirrors GENESIS_PLOTS in contracts/aether_claim.py).
export const GENESIS_PLOTS: Plot[] = [
  { plot_id: "CV-ALMOND-01", name: "Central Valley Almond Basin", crop_type: "ALMOND", latitude: 36.6, longitude: -119.8, latitude_e4: 366000, longitude_e4: -1198000, monthly_baselines: [50, 45, 35, 15, 8, 2, 0, 0, 4, 15, 25, 45], cadastral_hash: "7c1f0a52d19b3e6a8f4b0c2d9e57a31b6f8d40c7e92a5b1d3c68f0a47e2b9d15", crop_cycle: "Nov-Mar", crop_cycle_start_month: 11, crop_cycle_end_month: 3, active: true, registered_at: 0 },
  { plot_id: "IA-CORN-01", name: "Iowa Corn Belt", crop_type: "CORN", latitude: 42.03, longitude: -93.82, latitude_e4: 420300, longitude_e4: -938200, monthly_baselines: [25, 28, 55, 95, 120, 140, 115, 110, 85, 70, 50, 35], cadastral_hash: "b3a94e07c5d12f68a0e7b94c1d3f56a8e20c97b4d1f63a85c0e2b7194d6f3a08", crop_cycle: "Apr-Oct", crop_cycle_start_month: 4, crop_cycle_end_month: 10, active: true, registered_at: 0 },
  { plot_id: "AND-OLIVE-01", name: "Andalusia Olive Groves", crop_type: "OLIVE", latitude: 37.38, longitude: -4.37, latitude_e4: 373800, longitude_e4: -43700, monthly_baselines: [60, 55, 50, 45, 30, 10, 2, 3, 25, 60, 75, 70], cadastral_hash: "e51d7c3a9f08b246d1a5e83c70b9f4d2a6c18e5f307b94d2c1a8e6f05b3d7a49", crop_cycle: "Oct-Mar", crop_cycle_start_month: 10, crop_cycle_end_month: 3, active: true, registered_at: 0 },
];
export const GENESIS_POOL: Pool = {
  pool_balance: "0", total_liabilities: "0", committed_payouts: "0", free_capital: "0",
  epoch: 0, epoch_exposure: "0", max_epoch_exposure: (5n * ATTO).toString(),
  total_lp_shares: "0", share_price_atto: ATTO.toString(),
};

export interface LpPosition {
  shares: string;
  value: string;
  total_shares: string;
}

export const api = {
  plots: () => readOr<Plot[]>("list_plots", [], GENESIS_PLOTS),
  pool: () => readOr<Pool>("get_pool", [], GENESIS_POOL),
  policies: (holder: string) => readOr<Policy[]>("list_policies", [holder], []),
  lp: (holder: string) =>
    readOr<LpPosition>("get_lp", [holder], { shares: "0", value: "0", total_shares: "0" }),
  claim: (id: string) => readOr<Claim | Record<string, never>>("get_claim", [id], {}),
};

/**
 * First start date (UTC midnight) on or after `from` whose 90-day term passes
 * both the crop-cycle check and the window-floor check. Null if none within `maxDays`.
 */
export function nextInsurableStart(p: Plot, from = new Date(), maxDays = 400): Date | null {
  const DAY = 86400000;
  const base = Date.UTC(from.getUTCFullYear(), from.getUTCMonth(), from.getUTCDate());
  for (let d = 0; d <= maxDays; d++) {
    const start = new Date(base + d * DAY);
    if (termInsideCycle(p, start) && termWindowsInsurable(p, start)) return start;
  }
  return null;
}

/**
 * Plain-language note for a claim outcome. Settled tiers say which aggregation
 * produced them: the median of three answering sources, or the conservative
 * lower of two.
 */
export function outcomeNote(outcome: string, sourceCount: number): string {
  if (outcome === "QUORUM_FAILURE") return "too few sources, or a degraded 2-source reading not accepted · retry permitted until grace ends";
  if (outcome === "NORMAL") return "no drought · final, no payout";
  const basis = sourceCount === 3 ? "median of 3 sources" : "lower of 2 sources, accepted by the policyholder";
  return `${basis} · final`;
}
