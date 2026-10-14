/**
 * Deployment script for contracts/aether_claim.py (GenLayer Studio Next,
 * chain 61997, https://studio-next.genlayer.com/api).
 *
 *   genlayer network set studio-dev   # chain 61997 (same chain as Studio Next)
 *   genlayer deploy --rpc https://studio-next.genlayer.com/api
 *
 * `genlayer deploy` runs this file with a client already bound to the selected
 * network and account, so no key or chain object lives here.
 *
 * Seed registry: the three authenticated plots (Central Valley Almond Basin,
 * Iowa Corn Belt, Andalusia Olive Groves) are written into storage by the
 * contract constructor itself (GENESIS_PLOTS), so they are active from block
 * zero and the 24h staging delay only applies to plots added later. This
 * script verifies them by reading `list_plots` back.
 *
 * Fees: Studio Next has no on-chain FeeManager and the SDK resolves an absent
 * `fees` argument to a zero deposit, which the chain rejects with
 * FeeValueMustBeNonZero. `estimateTransactionFees` derives the deposit from
 * the live fee policy; it is applied to the deploy and the liquidity seed.
 */

import { readFileSync, writeFileSync, mkdirSync } from "fs";
import path from "path";

const CONTRACT_PATH = "contracts/aether_claim.py";
const ABI_PATH = "deployments/aether_claim_abi.json";
const DEPLOYMENT_PATH = "deployments/studio-next.json";
const RETRIES = 200;

const MAX_EPOCH_EXPOSURE = 5n * 10n ** 18n; // 5 GEN of coverage per epoch
const SEED_LIQUIDITY = 500_000_000_000_000_000n; // 0.5 GEN underwriter seed

interface Receipt {
  status?: number | string;
  statusName?: string;
  resultName?: string;
  txExecutionResult?: number;
  txExecutionResultName?: string;
  data?: { contract_address?: string };
  txDataDecoded?: { contractAddress?: string };
}
interface Fees {
  distribution: Record<string, unknown>;
  messageAllocations?: unknown;
  feeValue: bigint;
}
interface Client {
  initializeConsensusSmartContract(): Promise<void>;
  estimateTransactionFees(input: Record<string, never>): Promise<Fees>;
  deployContract(input: { code: Uint8Array; args: unknown[]; fees?: Fees }): Promise<string>;
  writeContract(input: {
    address: string;
    functionName: string;
    args: unknown[];
    value?: bigint;
    fees?: Fees;
  }): Promise<string>;
  readContract(input: { address: string; functionName: string; args: unknown[] }): Promise<unknown>;
  getContractSchema?(address: string): Promise<unknown>;
  waitForTransactionReceipt(input: {
    hash: string;
    waitUntil: "decided" | "finalized";
    retries: number;
  }): Promise<Receipt>;
}

const EXEC_NAMES = [
  "NOT_VOTED",
  "FINISHED_WITH_RETURN",
  "FINISHED_WITH_ERROR",
  "TIMEOUT",
  "NONDET_DISAGREE",
  "DETERMINISTIC_VIOLATION",
];

export type Outcome = "success" | "failed" | "unverified";

/**
 * ACCEPTED is not success: a transaction whose execution raised is still
 * accepted. Three outcomes, so a live deploy is never lost to an uncertain read:
 *   failed     not accepted, an execution error, or a consensus disagreement.
 *   success    accepted AND the receipt reports FINISHED_WITH_RETURN.
 *   unverified accepted, but the receipt does not report an execution result.
 * Only `failed` aborts. `unverified` is warned about and recorded as such.
 */
export function assessReceipt(r: Receipt): Outcome {
  const n = Number(r.status);
  // ACCEPTED and FINALIZED are both terminal-success *statuses*. (An earlier
  // revision of this check wrongly looked for "FINISHED", which is an execution
  // result, not a status.)
  const statusName = (r.statusName ?? "").toUpperCase();
  const accepted = n === 5 || n === 7 || statusName === "ACCEPTED" || statusName === "FINALIZED";
  if (!accepted) return "failed";
  const execName = (
    r.txExecutionResultName ?? (typeof r.txExecutionResult === "number" ? EXEC_NAMES[r.txExecutionResult] : "")
  ).toUpperCase();
  const bad = ["DISAGREE", "TIMEOUT", "DETERMINISTIC_VIOLATION", "NO_MAJORITY", "MAJORITY_DISAGREE", "MAJORITY_TIMEOUT"];
  if (r.resultName && bad.includes(r.resultName.toUpperCase())) return "failed";
  if (execName.endsWith("_WITH_ERROR") || ["TIMEOUT", "NONDET_DISAGREE", "DETERMINISTIC_VIOLATION"].includes(execName)) {
    return "failed";
  }
  // FINISHED_WITH_RETURN is what the SDK reports. FINALIZED_WITH_RETURN is
  // accepted too, so a receipt that reports finality is never misread.
  return execName.endsWith("_WITH_RETURN") ? "success" : "unverified";
}

function addressFrom(r: Receipt): string | undefined {
  if (r.txDataDecoded?.contractAddress) return r.txDataDecoded.contractAddress;
  if (r.data?.contract_address) return r.data.contract_address;
  const raw = r as unknown as Record<string, unknown>;
  const d = raw.contractAddress ?? raw.contract_address;
  return typeof d === "string" ? d : undefined;
}

const jsonSafe = (_k: string, v: unknown) => (typeof v === "bigint" ? v.toString() : v);

export default async function main(client: Client): Promise<string> {
  const code = new Uint8Array(readFileSync(path.resolve(process.cwd(), CONTRACT_PATH)));

  await client.initializeConsensusSmartContract();
  const fees = await client.estimateTransactionFees({});
  console.log(`Estimated fee deposit: ${fees.feeValue} wei`);

  const hash = await client.deployContract({ code, args: [MAX_EPOCH_EXPOSURE], fees });
  const receipt = await client.waitForTransactionReceipt({ hash, waitUntil: "decided", retries: RETRIES });
  const deployOutcome = assessReceipt(receipt);
  if (deployOutcome === "failed") throw new Error(`Deployment failed: ${JSON.stringify(receipt, jsonSafe)}`);
  if (deployOutcome === "unverified") {
    console.warn("WARNING: deploy receipt did not report an execution result. Verify the contract on the explorer.");
  }
  const address = addressFrom(receipt);
  if (!address) throw new Error(`No contract address in receipt: ${JSON.stringify(receipt, jsonSafe)}`);
  console.log(`AetherClaim deployed at ${address}`);

  // Persist the address first: a flaky read below must never lose a live deploy.
  mkdirSync(path.resolve(process.cwd(), "deployments"), { recursive: true });
  writeFileSync(
    path.resolve(process.cwd(), DEPLOYMENT_PATH),
    JSON.stringify(
      {
        network: "GenLayer Studio Next",
        chainId: 61997,
        rpc: "https://studio-next.genlayer.com/api",
        address,
        deployTx: hash,
        deployExecutionVerified: deployOutcome === "success",
        deployedAt: new Date().toISOString(),
      },
      jsonSafe,
      2,
    ),
  );

  // Verify the genesis registry came up authenticated and active. Studio's
  // gen_call has intermittently failed server-side, so this is best-effort.
  try {
    const plots = (await client.readContract({ address, functionName: "list_plots", args: [] })) as unknown;
    const list = Array.isArray(plots) ? plots : [];
    console.log(`Genesis registry: ${list.length} plots`);
    for (const p of list as Array<Record<string, unknown>>) {
      const m = p instanceof Map ? Object.fromEntries(p as Map<string, unknown>) : p;
      console.log(`  - ${m.plot_id}  ${m.name}  active=${m.active}`);
    }
  } catch (e) {
    console.log(`Registry read-back skipped (Studio read error): ${(e as Error).message?.slice(0, 120)}`);
  }

  // Seed underwriting liquidity (mints the first LP shares). Best-effort too.
  let seedHash = "";
  try {
    seedHash = await client.writeContract({
      address,
      functionName: "provide_liquidity",
      args: [],
      value: SEED_LIQUIDITY,
      fees,
    });
    const seed = await client.waitForTransactionReceipt({ hash: seedHash, waitUntil: "decided", retries: RETRIES });
    const seedOutcome = assessReceipt(seed);
    console.log(
      `Liquidity seed ${seedOutcome === "success" ? "executed" : seedOutcome === "unverified" ? "accepted, execution NOT verified" : "FAILED"} (${seedHash})`,
    );
    if (seedOutcome !== "failed") {
      // Record the seed alongside the deploy so the file is complete on its own.
      const record = JSON.parse(readFileSync(path.resolve(process.cwd(), DEPLOYMENT_PATH), "utf8"));
      writeFileSync(
        path.resolve(process.cwd(), DEPLOYMENT_PATH),
        JSON.stringify(
          { ...record, seedLiquidityTx: seedHash, seedExecutionVerified: seedOutcome === "success" },
          jsonSafe,
          2,
        ),
      );
    }
  } catch (e) {
    console.log(`Liquidity seed failed: ${(e as Error).message?.slice(0, 160)}`);
  }

  if (client.getContractSchema) {
    const schema = await client.getContractSchema(address);
    writeFileSync(path.resolve(process.cwd(), ABI_PATH), JSON.stringify(schema, jsonSafe, 2));
    console.log(`ABI written to ${ABI_PATH}`);
  }
  console.log(`Set NEXT_PUBLIC_AETHER_CLAIM_ADDRESS=${address} in frontend/.env.local`);
  return address;
}
