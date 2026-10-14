"use client";
import { useCallback, useEffect, useState } from "react";
import { api, Claim, EXPIRY_GRACE_DAYS, fromAtto, outcomeNote, Policy, TIERS, writeCall } from "@/lib/contract";
import { useWallet } from "@/lib/useAether";
import SyncBadge from "./SyncBadge";
import WalletSimNote from "./WalletSimNote";

export default function ClaimStream({ refreshKey }: { refreshKey: number }) {
  const { account, chainOk, connect } = useWallet();
  const [rows, setRows] = useState<{ policy: Policy; claim: Claim | null }[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [now, setNow] = useState(Math.floor(Date.now() / 1000));

  useEffect(() => {
    const t = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 1000);
    return () => clearInterval(t);
  }, []);

  const load = useCallback(async () => {
    if (!account) return setRows([]);
    setLoading(true);
    try {
      const pr = await api.policies(account);
      if (!pr.live) setMsg(null);
      const policies = pr.data;
      const withClaims = await Promise.all(
        policies.map(async (policy) => {
          const c = (await api.claim(policy.policy_id)).data as Claim;
          return { policy, claim: c && "outcome" in c ? c : null };
        }),
      );
      setRows(withClaims);
    } catch (e: any) {
      setMsg(e.message);
    } finally {
      setLoading(false);
    }
  }, [account]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const act = async (fn: string, id: string) => {
    if (!account) return connect();
    setBusy(id + fn);
    setMsg(null);
    try {
      const r = await writeCall(account, fn, [id]);
      setMsg(`${fn} · ${r.status} · ${r.hash.slice(0, 12)}…`);
      await load();
    } catch (e: any) {
      setMsg(e?.shortMessage ?? e?.message ?? "Transaction failed");
    } finally {
      setBusy(null);
    }
  };

  if (!account)
    return (
      <div className="card p-8 text-center">
        <p className="text-mist">Connect a wallet to see your policies and streaming payouts.</p>
        <button className="btn btn-solid mt-4" onClick={connect}>
          Connect wallet
        </button>
      </div>
    );

  return (
    <section className="space-y-5">
      {msg && <div className="card px-4 py-3 font-mono text-xs text-wheat">{msg}</div>}
      <div className="flex justify-end"><SyncBadge /></div>
      {loading && rows.length === 0 && <p className="text-sm text-mist">Querying chain (5s limit)…</p>}
      {!loading && rows.length === 0 && (
        <div className="card p-8 text-center text-mist">No policies for this account yet. Buy one in the Underwriting Desk.</div>
      )}
      {rows.map(({ policy, claim }) => {
        const approved = claim ? BigInt(claim.approved_payout) : 0n;
        const vested = claim ? BigInt(claim.vested_amount) : 0n;
        const cooldown = claim && approved > 0n ? claim.cooldown_until - now : 0;
        const linear =
          claim && approved > 0n && now > claim.cooldown_until
            ? Math.min(1, (now - claim.cooldown_until) / (claim.vesting_end - claim.cooldown_until))
            : 0;
        const releasedPct = approved > 0n ? Number((vested * 10000n) / approved) / 100 : 0;
        return (
          <article key={policy.policy_id} className="card p-5">
            <header className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="label">
                  {policy.policy_id} · {policy.plot_id}
                </div>
                <div className="font-display text-xl">{fromAtto(policy.coverage_amount, 3)} GEN coverage</div>
              </div>
              <span className="rounded-full border border-line px-3 py-1 font-mono text-xs">{policy.status}</span>
            </header>

            {claim && (
              <div className="mt-4 grid gap-3 sm:grid-cols-4">
                <Cell k="Outcome" v={claim.outcome} cls={claim.payout_tier > 0 ? TIERS[claim.payout_tier].color : ""} />
                <Cell k="Validator agreement" v={`${claim.source_count}/3 sources`} />
                <Cell k="Median deficit" v={`${claim.median_deficit}%`} />
                <Cell k="Approved" v={`${fromAtto(approved, 4)} GEN`} />
                <p className="font-mono text-[11px] text-wheat sm:col-span-4">{outcomeNote(claim.outcome, claim.source_count)}</p>
                <p className="font-mono text-[11px] text-mist sm:col-span-4">{claim.reasoning}</p>
              </div>
            )}

            {approved > 0n && claim && (
              <div className="mt-5">
                <div className="flex justify-between font-mono text-[11px] text-mist">
                  <span>{cooldown > 0 ? `cooldown · ${fmt(cooldown)} left` : "vesting"}</span>
                  <span>
                    released {fromAtto(vested, 4)} / {fromAtto(approved, 4)} GEN
                  </span>
                </div>
                <div className="relative mt-1 h-3 overflow-hidden rounded-full bg-loam">
                  <div className="absolute inset-y-0 left-0 bg-wheat/35" style={{ width: `${linear * 100}%` }} />
                  <div className="absolute inset-y-0 left-0 bg-sprout" style={{ width: `${releasedPct}%` }} />
                  {cooldown <= 0 && releasedPct < 100 && <div className="stream-bar absolute inset-y-0 left-0" style={{ width: `${Math.max(linear * 100, releasedPct)}%` }} />}
                </div>
                <div className="mt-1 flex justify-between font-mono text-[10px] text-mist">
                  <span>approved</span>
                  <span>+24h cooldown</span>
                  <span>+3d linear vesting</span>
                </div>
              </div>
            )}

            <footer className="mt-5 flex flex-wrap gap-3">
              {policy.status === "ACTIVE" && now <= policy.end_ts + EXPIRY_GRACE_DAYS * 86400 && (
                <button
                  className="btn"
                  disabled={!!busy || !chainOk || now < policy.matures_at}
                  onClick={() => act("evaluate_claim", policy.policy_id)}
                  title="Evaluation opens only after the fixed observation windows have matured"
                >
                  {busy === policy.policy_id + "evaluate_claim"
                    ? "Validators evaluating…"
                    : now < policy.matures_at
                      ? `Opens ${new Date(policy.matures_at * 1000).toISOString().slice(0, 10)}`
                      : "Trigger consensus check"}
                </button>
              )}
              {policy.status === "ACTIVE" && now > policy.end_ts + EXPIRY_GRACE_DAYS * 86400 && (
                <span className="font-mono text-xs text-rust">evaluation window closed · reserve releases on expiry</span>
              )}
              {policy.status === "SETTLED_NO_PAYOUT" && (
                <span className="font-mono text-xs text-mist">settled · no payout · final</span>
              )}
              {approved > 0n && (
                <button
                  className="btn btn-solid"
                  disabled={!!busy || !chainOk || cooldown > 0 || BigInt(claim!.releasable) === 0n}
                  onClick={() => act("release_payout", policy.policy_id)}
                >
                  Release {claim ? fromAtto(claim.releasable, 4) : "0"} GEN
                </button>
              )}
              <WalletSimNote />
            </footer>
          </article>
        );
      })}
    </section>
  );
}

function fmt(s: number) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h ${m}m`;
}

function Cell({ k, v, cls = "" }: { k: string; v: string; cls?: string }) {
  return (
    <div className="rounded-lg border border-line bg-loam p-3">
      <div className="label">{k}</div>
      <div className={`mt-1 font-mono text-xs ${cls}`}>{v}</div>
    </div>
  );
}
