"use client";
import { useEffect, useMemo, useState } from "react";
import { api, ATTO, fromAtto, GENESIS_PLOTS, GENESIS_POOL, PREMIUM_BPS, termInsideCycle, Plot, Pool, TIERS, toAtto, writeCall } from "@/lib/contract";
import SyncBadge from "./SyncBadge";
import WalletSimNote from "./WalletSimNote";
import { useWallet } from "@/lib/useAether";

export default function PolicyVault({ plotId, onChange }: { plotId: string | null; onChange: () => void }) {
  const { account, chainOk, connect } = useWallet();
  const [plots, setPlots] = useState<Plot[]>(GENESIS_PLOTS);
  const [pool, setPool] = useState<Pool>(GENESIS_POOL);
  const [plot, setPlot] = useState<string>(plotId ?? GENESIS_PLOTS[0].plot_id);
  const [coverage, setCoverage] = useState("0.5");
  const slider = Math.min(5, Math.max(0.01, parseFloat(coverage) || 0));
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = () => {
    api.plots().then((r) => setPlots(r.data));
    api.pool().then((r) => setPool(r.data));
  };
  useEffect(load, []);
  useEffect(() => {
    if (plotId) setPlot(plotId);
  }, [plotId]);

  const cov = useMemo(() => toAtto(coverage), [coverage]);
  const selectedPlot = plots.find((p) => p.plot_id === plot) ?? plots[0];
  const inSeason = selectedPlot ? termInsideCycle(selectedPlot) : true;
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

  const used = Number((BigInt(pool.epoch_exposure) * 100n) / (BigInt(pool.max_epoch_exposure) || 1n));

  return (
    <section className="grid gap-6 lg:grid-cols-5">
      <div className="card p-6 lg:col-span-3">
        <div className="flex items-center justify-between"><div className="label">Underwriting desk</div><SyncBadge /></div>
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
            <input
              type="range"
              min={0.01}
              max={5}
              step={0.01}
              value={slider}
              onChange={(e) => setCoverage(e.target.value)}
              className="mt-3 w-full accent-cyan-400"
              aria-label="Coverage amount"
            />
          </label>
        </div>

        <div className="mt-6 grid grid-cols-3 gap-3 text-center">
          <Stat k="Premium (8%)" v={`${fromAtto(premium, 5)} GEN`} />
          <Stat k="Max payout" v={`${fromAtto(cov, 3)} GEN`} />
          <Stat k="Term" v="90 days" />
        </div>

        <button className="btn btn-solid mt-6 w-full" disabled={busy || cov <= 0n || !inSeason || (!!account && !chainOk)} onClick={buy}>
          {!account ? "Connect wallet to buy" : busy ? "Awaiting consensus…" : `Pay ${fromAtto(premium, 5)} GEN premium`}
        </button>
        {!inSeason && selectedPlot && (
          <p className="mt-3 rounded-lg border border-rust/40 px-3 py-2 text-xs text-rust">
            Off-season: a 90-day term bought today would touch months outside {selectedPlot.name}&apos;s crop cycle
            ({selectedPlot.crop_cycle}). The contract rejects it so ordinary dry seasons cannot be insured.
          </p>
        )}
        <p className="mt-3 text-xs text-mist">The protocol admin address cannot hold policies (anti self-dealing).</p>
        <WalletSimNote />
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
                  ≥{t.min}% → <b className="text-white">{t.payoutBps / 100}%</b> · {fromAtto((cov * BigInt(t.payoutBps)) / 10000n, 3)} GEN
                </span>
              </div>
            ))}
          </div>
        </div>
        <div className="card p-5">
          <div className="label">Pool solvency</div>
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
