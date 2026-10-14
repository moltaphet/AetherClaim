"use client";
import { useEffect, useMemo, useState } from "react";
import { api, ATTO, fromAtto, PREMIUM_BPS, Plot, Pool, TIERS, toAtto, writeCall } from "@/lib/contract";
import { useWallet } from "@/lib/useAether";

export default function PolicyVault({ plotId, onChange }: { plotId: string | null; onChange: () => void }) {
  const { account, chainOk, connect } = useWallet();
  const [plots, setPlots] = useState<Plot[]>([]);
  const [pool, setPool] = useState<Pool | null>(null);
  const [plot, setPlot] = useState<string>(plotId ?? "");
  const [coverage, setCoverage] = useState("0.5");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = () => {
    api.plots().then((p) => setPlots(p)).catch(() => undefined);
    api.pool().then(setPool).catch(() => undefined);
  };
  useEffect(load, []);
  useEffect(() => {
    if (plotId) setPlot(plotId);
  }, [plotId]);

  const cov = useMemo(() => toAtto(coverage), [coverage]);
  const premium = (cov * PREMIUM_BPS) / 10000n;

  const buy = async () => {
    if (!account) return connect();
    setBusy(true);
    setMsg(null);
    try {
      const r = await writeCall(account, "create_policy", [plot || plots[0]?.plot_id, cov], premium);
      setMsg({ ok: true, text: `Policy submitted · ${r.status} · ${r.hash.slice(0, 12)}…` });
      load();
      onChange();
    } catch (e: any) {
      setMsg({ ok: false, text: e?.shortMessage ?? e?.message ?? "Transaction failed" });
    } finally {
      setBusy(false);
    }
  };

  const used = pool ? Number((BigInt(pool.epoch_exposure) * 100n) / (BigInt(pool.max_epoch_exposure) || 1n)) : 0;

  return (
    <section className="grid gap-6 lg:grid-cols-5">
      <div className="card p-6 lg:col-span-3">
        <div className="label">Underwriting desk</div>
        <h3 className="mt-1 font-display text-2xl">Buy a parametric drought policy</h3>
        <div className="mt-6 grid gap-5 sm:grid-cols-2">
          <label className="block">
            <span className="label">Registered plot</span>
            <select
              value={plot}
              onChange={(e) => setPlot(e.target.value)}
              className="mt-1 w-full rounded-lg border border-line bg-loam px-3 py-2 text-sm"
            >
              {plots.map((p) => (
                <option key={p.plot_id} value={p.plot_id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="label">Coverage (GEN)</span>
            <input
              value={coverage}
              onChange={(e) => setCoverage(e.target.value)}
              inputMode="decimal"
              className="mt-1 w-full rounded-lg border border-line bg-loam px-3 py-2 font-mono text-sm"
            />
          </label>
        </div>

        <div className="mt-6 grid grid-cols-3 gap-3 text-center">
          <Stat k="Premium (8%)" v={`${fromAtto(premium, 5)} GEN`} />
          <Stat k="Max payout" v={`${fromAtto(cov, 3)} GEN`} />
          <Stat k="Term" v="90 days" />
        </div>

        <button className="btn btn-solid mt-6 w-full" disabled={busy || cov <= 0n || (!!account && !chainOk)} onClick={buy}>
          {!account ? "Connect wallet to buy" : busy ? "Awaiting consensus…" : `Pay ${fromAtto(premium, 5)} GEN premium`}
        </button>
        {msg && <p className={`mt-3 text-sm ${msg.ok ? "text-sprout" : "text-rust"}`}>{msg.text}</p>}
        <p className="mt-4 text-xs text-mist">
          Observation windows must fall inside your coverage period — a policy bought after a drought cannot
          claim it. The first claim is possible ~34 days after purchase.
        </p>
      </div>

      <div className="space-y-6 lg:col-span-2">
        <div className="card p-5">
          <div className="label">Payout tiers (discrete)</div>
          <div className="mt-3 space-y-2">
            {TIERS.map((t) => (
              <div key={t.tier} className="flex items-center justify-between rounded-lg border border-line bg-loam px-3 py-2">
                <span className={`font-mono text-xs ${t.color}`}>{t.label}</span>
                <span className="font-mono text-xs text-mist">
                  deficit ≥ {t.min}% → <b className="text-white">{t.payoutBps / 100}%</b>
                </span>
              </div>
            ))}
          </div>
        </div>
        <div className="card p-5">
          <div className="label">Pool solvency</div>
          {pool ? (
            <>
              <div className="mt-2 grid grid-cols-2 gap-3 text-sm">
                <Stat k="Pool" v={`${fromAtto(pool.pool_balance, 3)} GEN`} />
                <Stat k="Reserved" v={`${fromAtto(pool.total_liabilities, 3)} GEN`} />
              </div>
              <div className="mt-4">
                <div className="flex justify-between font-mono text-[11px] text-mist">
                  <span>epoch {pool.epoch} exposure</span>
                  <span>
                    {fromAtto(pool.epoch_exposure, 2)} / {fromAtto(pool.max_epoch_exposure, 2)}
                  </span>
                </div>
                <div className="mt-1 h-2 rounded-full bg-loam">
                  <div className="h-2 rounded-full bg-wheat" style={{ width: `${Math.min(100, used)}%` }} />
                </div>
              </div>
            </>
          ) : (
            <p className="mt-2 text-sm text-mist">Loading pool…</p>
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

export { ATTO };
