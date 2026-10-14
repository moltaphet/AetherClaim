"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  ATTO,
  EXPIRY_GRACE_DAYS,
  fromAtto,
  GENESIS_POOL,
  LpPosition,
  MIN_DEPOSIT_ATTO,
  Notice,
  NOTICE_CLASS,
  noticeFromError,
  noticeFromWrite,
  parseGen,
  Policy,
  Pool,
  writeCall,
} from "@/lib/contract";
import { useWallet } from "@/lib/useAether";
import SyncAlert from "./SyncAlert";
import SyncBadge from "./SyncBadge";
import WalletSimNote from "./WalletSimNote";

/** ERC-4626-style underwriting vault: deposit GEN for shares, burn shares for a pro-rata slice. */
export default function LiquidityVault({ refreshKey }: { refreshKey: number }) {
  const { account, chainOk, connect } = useWallet();
  const [pool, setPool] = useState<Pool>(GENESIS_POOL);
  const [poolLive, setPoolLive] = useState(false);
  const [lp, setLp] = useState<LpPosition>({ shares: "0", value: "0", total_shares: "0" });
  const [lpLive, setLpLive] = useState(false);
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [policiesLive, setPoliciesLive] = useState(false);
  const [deposit, setDeposit] = useState("1");
  const [burn, setBurn] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [now, setNow] = useState(Math.floor(Date.now() / 1000));

  useEffect(() => {
    const t = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 30000);
    return () => clearInterval(t);
  }, []);

  const load = useCallback(() => {
    api.pool().then((r) => {
      setPoolLive(r.live);
      if (r.live) setPool(r.data);
    });
    api.policies("").then((r) => {
      setPoliciesLive(r.live);
      if (r.live) setPolicies(r.data);
    });
    if (account)
      api.lp(account).then((r) => {
        setLpLive(r.live);
        if (r.live) setLp(r.data);
      });
  }, [account]);
  useEffect(load, [load, refreshKey]);

  // ---- deposit ---------------------------------------------------------------
  const dep = parseGen(deposit, MIN_DEPOSIT_ATTO);
  const poolBal = BigInt(pool.pool_balance);
  const totalShares = BigInt(pool.total_lp_shares);
  const liabilities = BigInt(pool.total_liabilities);
  const mintPreview =
    dep.ok && poolLive
      ? totalShares === 0n || poolBal === 0n
        ? dep.value
        : (dep.value * totalShares) / poolBal
      : null;

  // ---- withdrawal breakdown, mirroring the contract's formula ---------------
  const burnParsed = burn.trim() === "" ? null : parseGen(burn, 1n);
  const myShares = BigInt(lp.shares);
  const breakdown = useMemo(() => {
    if (!burnParsed || !burnParsed.ok || !poolLive || totalShares === 0n) return null;
    const b = burnParsed.value;
    const headline = (b * poolBal) / totalShares; // pro-rata of the whole pool
    const free = poolBal > liabilities ? poolBal - liabilities : 0n;
    const cap = (b * free) / totalShares; // pro-rata of unreserved capital only
    const payout = headline < cap ? headline : cap;
    return { burn: b, headline, payout, retained: headline - payout };
  }, [burnParsed, poolLive, totalShares, poolBal, liabilities]);

  let burnBlock: string | null = null;
  if (burnParsed && !burnParsed.ok) burnBlock = burnParsed.error;
  else if (burnParsed && burnParsed.ok) {
    if (burnParsed.value > myShares) burnBlock = `Exceeds your ${fromAtto(myShares, 4)} shares`;
    else if (liabilities > 0n && burnParsed.value === totalShares)
      burnBlock = `The last LP cannot exit while policies are live (${fromAtto(liabilities, 4)} GEN reserved)`;
    else if (breakdown && breakdown.payout === 0n) burnBlock = "Nothing is withdrawable right now: all of this slice is reserved";
  }
  const canWithdraw = !!burnParsed && burnParsed.ok && !burnBlock && poolLive && lpLive;

  // ---- expired capital -------------------------------------------------------
  const expired = policies.filter((p) => p.status === "ACTIVE" && now > p.end_ts + EXPIRY_GRACE_DAYS * 86400);

  const run = async (label: string, fn: string, args: unknown[], value: bigint) => {
    if (!account) return connect();
    setBusy(label);
    setNotice(null);
    try {
      const r = await writeCall(account, fn, args, value);
      setNotice(noticeFromWrite(label, r));
      load();
    } catch (e) {
      setNotice(noticeFromError(e));
    } finally {
      setBusy(null);
    }
  };

  const price = BigInt(pool.share_price_atto || ATTO.toString());

  return (
    <section className="grid gap-6 lg:grid-cols-5">
      <div className="card p-6 lg:col-span-3">
        <div className="flex items-center justify-between">
          <div className="label">Underwriting vault</div>
          <SyncBadge />
        </div>
        <h3 className="mt-1 text-2xl font-semibold">Provide liquidity, earn premiums</h3>

        {!poolLive && (
          <div className="mt-4">
            <SyncAlert what="Vault state" />
          </div>
        )}

        <div className="mt-5 grid grid-cols-3 gap-3 text-center">
          <Stat k="Share price" v={poolLive ? `${fromAtto(price, 4)} GEN` : "—"} />
          <Stat k="Your shares" v={lpLive ? fromAtto(lp.shares, 4) : "—"} />
          <Stat k="Your value" v={lpLive ? `${fromAtto(lp.value, 4)} GEN` : "—"} />
        </div>

        <div className="mt-6 grid gap-5 sm:grid-cols-2">
          <div>
            <span className="label">Deposit (GEN)</span>
            <input
              value={deposit}
              onChange={(e) => setDeposit(e.target.value)}
              inputMode="decimal"
              aria-invalid={!dep.ok}
              className="mt-1 w-full rounded-lg border border-line bg-loam px-3 py-2 font-mono text-sm"
            />
            {!dep.ok ? (
              <p className="mt-1 font-mono text-[11px] text-rust">{dep.error}</p>
            ) : (
              <p className="mt-1 font-mono text-[11px] text-mist">
                {mintPreview !== null ? `≈ ${fromAtto(mintPreview, 4)} shares minted` : "share preview unavailable while syncing"}
              </p>
            )}
            <button
              className="btn btn-solid mt-3 w-full"
              disabled={!!busy || !dep.ok || (!!account && !chainOk)}
              onClick={() => dep.ok && run("provide_liquidity", "provide_liquidity", [], dep.value)}
            >
              {!account ? "Connect wallet" : busy === "provide_liquidity" ? "Awaiting consensus…" : "Deposit"}
            </button>
          </div>
          <div>
            <span className="label">Burn shares</span>
            <input
              value={burn}
              onChange={(e) => setBurn(e.target.value)}
              placeholder={lpLive ? fromAtto(lp.shares, 4) : "shares"}
              inputMode="decimal"
              aria-invalid={!!burnBlock}
              className="mt-1 w-full rounded-lg border border-line bg-loam px-3 py-2 font-mono text-sm"
            />
            {burn.trim() !== "" && burnBlock ? (
              <p className="mt-1 font-mono text-[11px] text-rust">{burnBlock}</p>
            ) : (
              <p className="mt-1 font-mono text-[11px] text-mist">pays pro-rata of unreserved capital</p>
            )}
            <button
              className="btn mt-3 w-full"
              disabled={!!busy || !canWithdraw || (!!account && !chainOk)}
              onClick={() => burnParsed && burnParsed.ok && run("withdraw_liquidity", "withdraw_liquidity", [burnParsed.value], 0n)}
            >
              {busy === "withdraw_liquidity" ? "Awaiting consensus…" : "Withdraw"}
            </button>
          </div>
        </div>

        {breakdown && !burnBlock && (
          <div className="mt-5 rounded-lg border border-line bg-loam p-4 text-sm">
            <div className="label">Withdrawal breakdown</div>
            <dl className="mt-2 space-y-2">
              <div className="flex justify-between">
                <dt className="text-mist">Expected payout (unreserved capital)</dt>
                <dd className="font-mono text-sprout">{fromAtto(breakdown.payout, 4)} GEN</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-mist">Pro-rata value of these shares</dt>
                <dd className="font-mono">{fromAtto(breakdown.headline, 4)} GEN</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-mist">Stays locked in the pool</dt>
                <dd className="font-mono text-wheat">{fromAtto(breakdown.retained, 4)} GEN</dd>
              </div>
            </dl>
            {liabilities > 0n && (
              <p className="mt-3 text-xs text-wheat">
                {fromAtto(liabilities, 4)} GEN of the pool is reserved against active policies.
                {breakdown.retained > 0n
                  ? ` ${fromAtto(breakdown.retained, 4)} GEN of this slice remains locked against active policies until they settle or expire. That part is not paid out later: burning these shares leaves it in the pool for the remaining LPs. Burn fewer shares, or wait for the policies to settle, to keep it.`
                  : " This slice is fully covered by unreserved capital."}
              </p>
            )}
          </div>
        )}

        {notice && <p className={`mt-4 text-sm ${NOTICE_CLASS[notice.tone]}`}>{notice.text}</p>}
        <WalletSimNote />
      </div>

      <div className="space-y-6 lg:col-span-2">
        <div className="card p-5">
          <div className="label">Vault accounting</div>
          {poolLive ? (
            <dl className="mt-3 space-y-3 text-sm">
              <Row k="Pool balance" v={`${fromAtto(pool.pool_balance, 4)} GEN`} />
              <Row k="Reserved against policies" v={`${fromAtto(pool.total_liabilities, 4)} GEN`} />
              <Row
                k="Unreserved (withdrawable)"
                v={`${fromAtto(poolBal > liabilities ? poolBal - liabilities : 0n, 4)} GEN`}
              />
              <Row k="Total shares" v={fromAtto(pool.total_lp_shares, 4)} />
            </dl>
          ) : (
            <div className="mt-3">
              <SyncAlert what="Pool accounting" />
            </div>
          )}
          <ul className="mt-5 space-y-1 text-xs text-mist">
            <li>Premiums raise the share price; paid claims lower it, for every LP equally.</li>
            <li>Exits are capped to the exiting shares&apos; slice of unreserved capital, so nobody can step out ahead of a loss.</li>
          </ul>
        </div>

        <div className="card p-5">
          <div className="label">Release expired capital</div>
          {!policiesLive ? (
            <div className="mt-3">
              <SyncAlert what="The policy list" />
            </div>
          ) : expired.length === 0 ? (
            <p className="mt-3 text-xs text-mist">
              No policy is past its {EXPIRY_GRACE_DAYS}-day grace period. Reserves release automatically once a policy settles,
              or when it expires unclaimed and anyone calls expiry.
            </p>
          ) : (
            <ul className="mt-3 space-y-2">
              {expired.map((p) => (
                <li key={p.policy_id} className="flex items-center justify-between gap-3 rounded-lg border border-line bg-loam px-3 py-2">
                  <span className="font-mono text-xs">
                    {p.policy_id} · {fromAtto(p.coverage_amount, 3)} GEN reserved
                  </span>
                  <button
                    className="btn px-3 py-1 text-xs"
                    disabled={!!busy || (!!account && !chainOk)}
                    onClick={() => run(`expire ${p.policy_id}`, "expire_policy", [p.policy_id], 0n)}
                  >
                    {busy === `expire ${p.policy_id}` ? "Releasing…" : "Release Expired Capital"}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
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
