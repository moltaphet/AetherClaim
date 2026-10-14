// AetherClaim contract bindings: dual-RPC reads with timeout guards, and
// wallet writes with explicit gas / fee overrides.

export const CHAIN_ID = 61997;
export const CHAIN_ID_HEX = "0xf22d";
export const CONTRACT_ADDRESS =
  process.env.NEXT_PUBLIC_AETHER_CLAIM_ADDRESS ?? "0xD368f35b238d84Bfd1024441992Cc2d04276bf79";

// Primary first, fallback second. Both serve chain 61997.
export const RPC_URLS = [
  "https://studio-next.genlayer.com/api",
  "https://studio-dev.genlayer.com/api",
];
export const EXPLORER = "https://explorer-studio-next.genlayer.com";

export const READ_TIMEOUT_MS = 9000;
export const TX_GAS_LIMIT = 500000; // explicit, so MetaMask never simulates GenVM
export const TX_GAS_LIMIT_HEX = "0x" + TX_GAS_LIMIT.toString(16);
export const TX_FEE_HEX = "0x3b9aca00"; // 1 gwei max fee & priority fee

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
let preferred = 0;

/** Call a view on the primary RPC; on error/timeout fall back to the next one. */
export async function readView<T = unknown>(functionName: string, args: unknown[] = []): Promise<T> {
  let lastErr: unknown;
  for (let i = 0; i < RPC_URLS.length; i++) {
    const idx = (preferred + i) % RPC_URLS.length;
    const rpc = RPC_URLS[idx];
    try {
      let client = readClients.get(rpc);
      if (!client) {
        client = await buildClient(rpc);
        readClients.set(rpc, client);
      }
      const out = await withTimeout(
        client.readContract({ address: CONTRACT_ADDRESS, functionName, args }),
        READ_TIMEOUT_MS,
        `${functionName}@${new URL(rpc).host}`,
      );
      preferred = idx;
      return normalise(out) as T;
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error(String(lastErr));
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
 * Wraps the injected provider so every eth_sendTransaction carries an explicit
 * gas limit and EIP-1559 fee fields. MetaMask then skips its own estimation,
 * which cannot simulate GenVM and would otherwise flag the transaction.
 */
export function withGasOverrides(p: Eip1193): Eip1193 {
  return {
    ...p,
    request: (a) => {
      if (a.method === "eth_sendTransaction" && a.params?.[0]) {
        const tx = { ...(a.params[0] as Record<string, unknown>) };
        tx.gas = TX_GAS_LIMIT_HEX;
        delete tx.gasPrice;
        tx.maxFeePerGas = TX_FEE_HEX;
        tx.maxPriorityFeePerGas = TX_FEE_HEX;
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
  const client = await buildClient(RPC_URLS[0], account, withGasOverrides(p));
  let fees: unknown;
  if (typeof client.estimateTransactionFees === "function") {
    // Studio Next has no FeeManager; an absent preset becomes a zero deposit,
    // which the chain rejects. Derive it from the live policy instead.
    fees = await withTimeout(client.estimateTransactionFees({}), READ_TIMEOUT_MS, "estimateTransactionFees");
  }
  const hash = (await client.writeContract({
    address: CONTRACT_ADDRESS,
    functionName,
    args,
    value,
    fees,
  })) as string;
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
  baseline_rainfall_mm: number;
  cadastral_hash: string;
  crop_cycle: string;
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
}

export const api = {
  plots: () => readView<Plot[]>("list_plots"),
  pool: () => readView<Pool>("get_pool"),
  policies: (holder: string) => readView<Policy[]>("list_policies", [holder]),
  claim: (id: string) => readView<Claim | Record<string, never>>("get_claim", [id]),
};
