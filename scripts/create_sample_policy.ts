/**
 * Sends the live sample `create_policy` from the ELIGIBLE operator account and
 * records the result in deployments/studio-next.json. The admin cannot hold
 * policies, so this must run with the farmer account active.
 *
 * `genlayer deploy` executes every script in the ./deploy folder of its working
 * directory, so run it from a directory that contains only this script:
 *
 *   mkdir -p /tmp/run/deploy && cp scripts/create_sample_policy.ts /tmp/run/deploy/
 *   genlayer account use aetherclaim-farmer
 *   (cd /tmp/run && PROJECT_ROOT=$PWD_OF_REPO genlayer deploy)
 */
import { readFileSync, writeFileSync } from "fs";
import path from "path";

const ROOT = process.env.PROJECT_ROOT ?? process.cwd();
const RECORD = path.resolve(ROOT, "deployments/studio-next.json");
const PLOT = "AND-OLIVE-01"; // Oct-Mar crop cycle: insurable from mid-October
const COVERAGE = 100_000_000_000_000_000n; // 0.1 GEN, under the 25% pool concentration cap
const PREMIUM = (COVERAGE * 800n) / 10000n; // 8%

const jsonSafe = (_k: string, v: unknown) => (typeof v === "bigint" ? v.toString() : v);

interface Client {
  estimateTransactionFeesForWrite?(i: Record<string, unknown>): Promise<Record<string, unknown>>;
  estimateTransactionFees(i: Record<string, never>): Promise<Record<string, unknown>>;
  writeContract(i: Record<string, unknown>): Promise<string>;
  readContract(i: Record<string, unknown>): Promise<unknown>;
  waitForTransactionReceipt(i: Record<string, unknown>): Promise<Record<string, unknown>>;
}

export default async function main(client: Client): Promise<void> {
  const record = JSON.parse(readFileSync(RECORD, "utf8"));
  const address: string = record.address;
  const target = { address, functionName: "create_policy", args: [PLOT, COVERAGE], value: PREMIUM };
  let fees: Record<string, unknown>;
  try {
    fees = await client.estimateTransactionFeesForWrite!(target);
  } catch {
    fees = await client.estimateTransactionFees({});
  }
  const hash = await client.writeContract({ ...target, fees });
  const receipt = await client.waitForTransactionReceipt({ hash, waitUntil: "decided", retries: 200 });
  console.log(`create_policy tx ${hash}`);
  console.log(JSON.stringify({ status: receipt.statusName, exec: receipt.txExecutionResultName, result: receipt.resultName }));

  const readbacks: Record<string, unknown> = {};
  for (const [fn, args] of [
    ["list_policies", [""]],
    ["get_policy", ["P-1"]],
    ["get_plot", [PLOT]],
    ["get_pool", []],
  ] as Array<[string, unknown[]]>) {
    try {
      readbacks[fn] = await client.readContract({ address, functionName: fn, args });
    } catch (e) {
      readbacks[fn] = { error: String((e as Error).message).slice(0, 300) };
    }
  }
  const prior = JSON.parse(readFileSync(RECORD, "utf8"));
  writeFileSync(
    RECORD,
    JSON.stringify(
      {
        ...prior,
        samplePolicyTx: hash,
        samplePolicy: { plot: PLOT, coverage: COVERAGE, premium: PREMIUM, execution: receipt.txExecutionResultName },
        samplePolicyReadbacks: readbacks,
        samplePolicyAt: new Date().toISOString(),
      },
      jsonSafe,
      2,
    ),
  );
}
