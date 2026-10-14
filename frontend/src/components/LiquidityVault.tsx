"use client";
import { useCallback, useEffect, useState } from "react";
import { api, ATTO, fromAtto, GENESIS_POOL, LpPosition, Pool, toAtto, writeCall } from "@/lib/contract";
import { useWallet } from "@/lib/useAether";
import SyncBadge from "./SyncBadge";
import WalletSimNote from "./WalletSimNote";

/** ERC-4626-style underwriting vault: deposit GEN for shares, burn shares for a pro-rata slice. */
export default function LiquidityVault({ refreshKey }: { refreshKey: number }) {
  const { account, chainOk, connect } = useWallet();
  const [pool, setPool] = useState<Pool>(GENESIS_POOL);
  const [lp, setLp] = useState<LpPosition>({ shares: "0", value: "0", total_shares: "0" });
  const [deposit, setDeposit] = useState("1");
  const [burn, setBurn] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(() => {
    api.pool().then((r) => setPool(r.data));
    if (account) api.lp(account).then((r) => setLp(r.data));
  }, [account]);
  useEffect(load, [load, refreshKey]);

  const price = BigInt(pool.share_price_atto || ATTO.toString());
  const depositAtto = toAtto(deposit);
  const mintPreview = BigInt(pool.total_lp_shares) === 0n || BigInt(pool.pool_balance) === 0n
    ? depositAtto
    : (depositAtto * BigInt(pool.total_lp_shares)) / BigInt(pool.pool_balance);
  const free = BigInt(pool.pool_balance) - BigInt(pool.total_liabilities);

  const run = async (fn: string, args: unknown[], value: bigint) => {
    if (!account) return connect();
    setBusy(true);
    setMsg(null);
    try {
      const r = await writeCall(account, fn, args, value);
      setMsg({ ok: true, text: `${fn} · ${r.status} · ${r.hash.slice(0, 12)}…` });
      load();
    } catch (e: any) {
      setMsg({ ok: false, text: e?.shortMessage ?? e?.message ?? "Transaction failed" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="grid gap-6 lg:grid-cols-5">
      <div className="card p-6 lg:col-span-3">
        <div className="flex items-center justify-between">
          <div className="label">Underwriting vault</div>
          <SyncBadge />
        </div>
        <h3 className="mt-1 text-2xl font-semibold">Provide liquidity, earn premiums</h3>

        <div className="mt-5 grid grid-cols-3 gap-3 text-center">
          <Stat k="Share price" v={`${fromAtto(price, 4)} GEN`} />
          <Stat k="Your shares" v={fromAtto(lp.shares, 4)} />
          <Stat k="Your value" v={`${fromAtto(lp.value, 4)} GEN`} />
        </div>

        <div className="mt-6 grid gap-5 sm:grid-cols-2">
          <div>
            <span className="label">Deposit (GEN)</span>
            <input
              value={deposit}
              onChange={(e) => setDeposit(e.target.value)}
              inputMode="decimal"
              className="mt-1 w-full rounded-lg border border-line bg-loam px-3 py-2 font-mono text-sm"
            />
            <p className="mt-1 font-mono text-[11px] text-mist">≈ {fromAtto(mintPreview, 4)} shares minted</p>
            <button
              className="btn btn-solid mt-3 w-full"
              disabled={busy || depositAtto <= 0n || (!!account && !chainOk)}
              onClick={() => run("provide_liquidity", [], depositAtto)}
            >
              {!account ? "Connect wallet" : busy ? "Awaiting consensus…" : "Deposit"}
            </button>
          </div>
          <div>
            <span className="label">Burn shares</span>
            <input
              value={burn}
              onChange={(e) => setBurn(e.target.value)}
              placeholder={fromAtto(lp.shares, 4)}
              inputMode="decimal"
              className="mt-1 w-full rounded-lg border border-line bg-loam px-3 py-2 font-mono text-sm"
            />
            <p className="mt-1 font-mono text-[11px] text-mist">pays pro-rata of unreserved capital</p>
            <button
              className="btn mt-3 w-full"
              disabled={busy || BigInt(lp.shares) === 0n || (!!account && !chainOk)}
              onClick={() => run("withdraw_liquidity", [burn ? toAtto(burn) : BigInt(lp.shares)], 0n)}
            >
              Withdraw
            </button>
          </div>
        </div>
        {msg && <p className={`mt-4 text-sm ${msg.ok ? "text-sprout" : "text-rust"}`}>{msg.text}</p>}
        <WalletSimNote />
      </div>

      <div className="card p-5 lg:col-span-2">
        <div className="label">Vault accounting</div>
        <dl className="mt-3 space-y-3 text-sm">
          <Row k="Pool balance" v={`${fromAtto(pool.pool_balance, 4)} GEN`} />
          <Row k="Reserved against policies" v={`${fromAtto(pool.total_liabilities, 4)} GEN`} />
          <Row k="Unreserved (withdrawable)" v={`${fromAtto(free < 0n ? 0n : free, 4)} GEN`} />
          <Row k="Total shares" v={fromAtto(pool.total_lp_shares, 4)} />
        </dl>
        <ul className="mt-5 space-y-1 text-xs text-mist">
          <li>Premiums raise the share price; paid claims lower it, for every LP equally.</li>
          <li>Exits are capped to the exiting shares&apos; slice of unreserved capital, so nobody can step out ahead of a loss.</li>
        </ul>
      </div>
    </section>
  );
}

function Stat({ k, v }: { k: string; v: string }) {
  return (
    <div className="rounded-lg border border-line bg-loam p-3">
      <div className="label">{k}</div>
      <div className="mt-1 font-mono text-sm">{v}</div>
    </div>
  );
}
function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between border-b border-line/60 pb-2">
      <dt className="text-mist">{k}</dt>
      <dd className="font-mono">{v}</dd>
    </div>
  );
}
