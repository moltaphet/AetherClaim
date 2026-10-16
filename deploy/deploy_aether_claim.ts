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
 * Cadastral authority: the Ed25519 public key in registry/attestations.json is
 * passed to the constructor and is immutable. `verify_plot_provenance` needs the
 * authority's signature over each plot, so the admin who runs this script cannot
 * make a plot insurable by itself. The private key never touches this script.
 *
 * Seed registry: the three genesis plots (Central Valley Almond Basin, Iowa Corn
 * Belt, Andalusia Olive Groves) are written into storage by the constructor
 * (GENESIS_PLOTS), bound to the eligible farm operator named in
 * registry/cadastral_manifest.json. They are NOT insurable until validators have
 * matched each one to that manifest: this script sends one
 * `verify_plot_provenance` transaction per plot and records the readback.
 *
 * The sample policy (`create_policy`) must be signed by the ELIGIBLE operator,
 * not the admin, so it is a separate step (see README "Reproducing the live run").
 *
 * Fees: Studio Next has no on-chain FeeManager and the SDK resolves an absent
 * `fees` argument to a zero deposit, which the chain rejects with
 * FeeValueMustBeNonZero. `estimateTransactionFees` derives the deposit from
 * the live fee policy; it is applied to the deploy and the liquidity seed.
 */

import { createHash } from "crypto";
import { readFileSync, writeFileSync, mkdirSync } from "fs";
import path from "path";

const CONTRACT_PATH = "contracts/aether_claim.py";
const ABI_PATH = "deployments/aether_claim_abi.json";
const DEPLOYMENT_PATH = "deployments/studio-next.json";
const MANIFEST_PATH = "registry/cadastral_manifest.json";
const ATTESTATIONS_PATH = "registry/attestations.json";
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
  const codeBuf = readFileSync(path.resolve(process.cwd(), CONTRACT_PATH));
  const code = new Uint8Array(codeBuf);
  const sourceSha256 = createHash("sha256").update(codeBuf).digest("hex");
  const manifestBuf = readFileSync(path.resolve(process.cwd(), MANIFEST_PATH));
  const manifest = JSON.parse(manifestBuf.toString("utf8")) as {
    plots: Record<string, { eligible_policyholder: string }>;
  };
  const plotIds = Object.keys(manifest.plots);
  const attBuf = readFileSync(path.resolve(process.cwd(), ATTESTATIONS_PATH));
  const att = JSON.parse(attBuf.toString("utf8")) as {
    authority_public_key: string;
    attestations: Record<string, { registry_record_id: string; signature: string; cadastral_hash: string }>;
  };
  for (const id of plotIds) {
    if (!att.attestations[id]) throw new Error(`no cadastral attestation for ${id}`);
  }
  const operators = new Set(plotIds.map((id) => manifest.plots[id].eligible_policyholder.toLowerCase()));
  if (operators.size !== 1) throw new Error("manifest must name exactly one genesis operator");
  const genesisPolicyholder = [...operators][0];
  console.log(`Source sha256 ${sourceSha256}; genesis operator ${genesisPolicyholder}`);

  await client.initializeConsensusSmartContract();
  const fees = await client.estimateTransactionFees({});
  console.log(`Estimated fee deposit: ${fees.feeValue} wei`);

  const hash = await client.deployContract({ code, args: [MAX_EPOCH_EXPOSURE, genesisPolicyholder, att.authority_public_key], fees });
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
        sourcePath: CONTRACT_PATH,
        sourceSha256,
        manifestSha256: createHash("sha256").update(manifestBuf).digest("hex"),
        genesisPolicyholder,
        cadastralAuthority: att.authority_public_key,
        attestationsSha256: createHash("sha256").update(attBuf).digest("hex"),
      },
      jsonSafe,
      2,
    ),
  );

  const record = () => JSON.parse(readFileSync(path.resolve(process.cwd(), DEPLOYMENT_PATH), "utf8"));
  const save = (patch: Record<string, unknown>) =>
    writeFileSync(path.resolve(process.cwd(), DEPLOYMENT_PATH), JSON.stringify({ ...record(), ...patch }, jsonSafe, 2));

  // Adversarial check, live: present the authority's genuine signature for ANOTHER
  // plot. The contract must revert with ERR_INVALID_CADASTRAL_ATTESTATION.
  if (plotIds.length >= 2) {
    const [target, donor] = [plotIds[plotIds.length - 1], plotIds[0]];
    try {
      const h = await client.writeContract({
        address,
        functionName: "verify_plot_provenance",
        args: [target, att.attestations[donor].registry_record_id, att.attestations[donor].signature],
        fees,
      });
      const r = await client.waitForTransactionReceipt({ hash: h, waitUntil: "decided", retries: RETRIES });
      const outcome = assessReceipt(r);
      console.log(`mismatched attestation for ${target}: ${outcome} (${h}) - expected "failed"`);
      save({ rejectedAttestationTx: { plot: target, signatureOf: donor, tx: h, outcome, expected: "failed" } });
    } catch (e) {
      console.log(`mismatched attestation rejected at submission: ${(e as Error).message?.slice(0, 160)}`);
      save({ rejectedAttestationTx: { plot: target, signatureOf: donor, rejectedAt: "submission", error: (e as Error).message?.slice(0, 300) } });
    }
  }

  // Independent plot provenance: the authority's signature is checked on-chain,
  // then validators fetch the pinned cadastral manifest and match every field.
  // Permissionless; anything else leaves the plot uninsurable.
  const provenanceTxs: Record<string, string> = {};
  for (const plotId of plotIds) {
    try {
      const a = att.attestations[plotId];
      const h = await client.writeContract({
        address,
        functionName: "verify_plot_provenance",
        args: [plotId, a.registry_record_id, a.signature],
        fees,
      });
      const r = await client.waitForTransactionReceipt({ hash: h, waitUntil: "decided", retries: RETRIES });
      provenanceTxs[plotId] = h;
      console.log(`verify_plot_provenance(${plotId}): ${assessReceipt(r)} (${h})`);
    } catch (e) {
      console.log(`verify_plot_provenance(${plotId}) failed: ${(e as Error).message?.slice(0, 160)}`);
    }
  }
  save({ provenanceTxs });

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
      save({ seedLiquidityTx: seedHash, seedExecutionVerified: seedOutcome === "success" });
    }
  } catch (e) {
    console.log(`Liquidity seed failed: ${(e as Error).message?.slice(0, 160)}`);
  }

  // Authoritative readbacks. Studio's gen_call has intermittently failed
  // server-side, so each is best-effort and its failure is recorded verbatim.
  const readbacks: Record<string, unknown> = {};
  for (const [fn, args] of [
    ["list_plots", []],
    ["get_pool", []],
    ["get_weather_sources", []],
    ["get_config", []],
  ] as Array<[string, unknown[]]>) {
    try {
      readbacks[fn] = await client.readContract({ address, functionName: fn, args });
      console.log(`readback ${fn}: ok`);
    } catch (e) {
      readbacks[fn] = { error: (e as Error).message?.slice(0, 300) };
      console.log(`readback ${fn} failed: ${(e as Error).message?.slice(0, 120)}`);
    }
  }
  save({ readbacks, readbacksAt: new Date().toISOString() });

  if (client.getContractSchema) {
    const schema = await client.getContractSchema(address);
    writeFileSync(path.resolve(process.cwd(), ABI_PATH), JSON.stringify(schema, jsonSafe, 2));
    console.log(`ABI written to ${ABI_PATH}`);
  }
  console.log(`Set NEXT_PUBLIC_AETHER_CLAIM_ADDRESS=${address} in frontend/.env.local`);
  return address;
}
