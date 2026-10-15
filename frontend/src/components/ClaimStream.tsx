"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  Claim,
  EXPIRY_GRACE_DAYS,
  fromAtto,
  Notice,
  NOTICE_CLASS,
  noticeFromError,
  noticeFromWrite,
  outcomeNote,
  Policy,
  TIERS,
  writeCall,
} from "@/lib/contract";
import { useWallet } from "@/lib/useAether";
import { canFinalize, conservativeTier, MAX_REVISIONS, revisionState } from "@/lib/underwriting";
import SyncAlert from "./SyncAlert";
import SyncBadge from "./SyncBadge";
import WalletSimNote from "./WalletSimNote";

export default function ClaimStream({ refreshKey }: { refreshKey: number }) {
  const { account, chainOk, connect } = useWallet();
  const [rows, setRows] = useState<{ policy: Policy; claim: Claim | null; claimLive: boolean }[]>([]);
  const [loading, setLoading] = useState(false);
  // false means the last policy read failed. Rows keep their last known value
  // rather than being replaced by an empty list that would read as "no policies".
  const [live, setLive] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [refetched, setRefetched] = useState<Record<string, boolean>>({});
  const [now, setNow] = useState(Math.floor(Date.now() / 1000));

  useEffect(() => {
    const t = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 1000);
    return () => clearInterval(t);
  }, []);

  // Every load gets a ticket. A response from a previous address, or from before
  // an unmount, is discarded rather than rendered under the new account.
  const ticket = useRef(0);
  useEffect(() => {
    const t = ticket;
    return () => {
      t.current += 1;
    };
  }, []);

  const load = useCallback(async () => {
    const mine = ++ticket.current;
    if (!account) {
      setRows([]);
      return;
    }
    setLoading(true);
    try {
      const pr = await api.policies(account);
      if (mine !== ticket.current) return;
      setLive(pr.live);
      if (!pr.live) return; // keep the last known rows, show the sync alert
      const withClaims = await Promise.all(
        pr.data.map(async (policy) => {
          const cr = await api.claim(policy.policy_id);
          const c = cr.data as Claim;
          return { policy, claim: cr.live && c && "outcome" in c ? c : null, claimLive: cr.live };
        }),
      );
      if (mine !== ticket.current) return;
      setRows(withClaims);
      // Claims already past their cooldown at load time need no extra refetch.
      const nowSec = Math.floor(Date.now() / 1000);
      const passed = withClaims.filter(
        ({ claim }) => claim && claim.cooldown_until > 0 && nowSec >= claim.cooldown_until,
      );
      setRefetched((r) => ({ ...r, ...Object.fromEntries(passed.map((d) => [d.policy.policy_id, true])) }));
    } catch {
      if (mine === ticket.current) setLive(false);
    } finally {
      if (mine === ticket.current) setLoading(false);
    }
  }, [account]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const act = async (fn: string, id: string) => {
    if (!account) return connect();
    setBusy(id + fn);
    setNotice(null);
    try {
      const r = await writeCall(account, fn, [id]);
      setNotice(noticeFromWrite(fn, r));
      await load();
    } catch (e) {
      setNotice(noticeFromError(e));
    } finally {
      setBusy(null);
    }
  };

  // When a cooldown reaches zero, re-read the chain once so the release action
  // reflects authoritative claimable state without a page refresh.
  useEffect(() => {
    const due = rows.filter(
      ({ claim, policy }) =>
        claim &&
        BigInt(claim.approved_payout) > 0n &&
        claim.cooldown_until > 0 &&
        now >= claim.cooldown_until &&
        !refetched[policy.policy_id],
    );
    if (due.length === 0) return;
    setRefetched((r) => ({ ...r, ...Object.fromEntries(due.map((d) => [d.policy.policy_id, true])) }));
    load();
  }, [now, rows, refetched, load]);

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
      {notice && <div className={`card px-4 py-3 font-mono text-xs ${NOTICE_CLASS[notice.tone]}`}>{notice.text}</div>}
      <div className="flex justify-end"><SyncBadge /></div>
      {!live && <SyncAlert what="Your policy list" />}
      {loading && rows.length === 0 && live && <p className="text-sm text-mist">Connecting… loading policies for this account (5s limit)</p>}
      {!loading && live && rows.length === 0 && (
        <div className="card p-8 text-center text-mist">No policies found for this account. Buy one in the Underwriting Desk.</div>
      )}
      {rows.map(({ policy, claim, claimLive }) => {
        const approved = claim ? BigInt(claim.approved_payout) : 0n;
        const vested = claim ? BigInt(claim.vested_amount) : 0n;
        const cooldown = claim && approved > 0n ? claim.cooldown_until - now : 0;
        const linear =
          claim && approved > 0n && now > claim.cooldown_until
            ? Math.min(1, (now - claim.cooldown_until) / (claim.vesting_end - claim.cooldown_until))
            : 0;
        const releasedPct = approved > 0n ? Number((vested * 10000n) / approved) / 100 : 0;
        // Releasable is recomputed from the clock so the button enables the moment
        // the cooldown ends, not only after the next chain read. Same formula as the contract.
        const VEST = 3 * 86400;
        const elapsed = claim && approved > 0n ? Math.max(0, Math.min(now - claim.cooldown_until, VEST)) : 0;
        const vestedNow = approved > 0n ? (approved * BigInt(elapsed)) / BigInt(VEST) : 0n;
        const releasableNow = vestedNow > vested ? vestedNow - vested : 0n;
        const revision = revisionState(claim, now);
        const finalizable = canFinalize(claim, now);
        const matured = now >= policy.matures_at;
        const inGrace = now <= policy.end_ts + EXPIRY_GRACE_DAYS * 86400;
        const reserve = claim?.revision_reserve ? BigInt(claim.revision_reserve) : 0n;
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

            {!claimLive && (
              <p className="mt-3 font-mono text-[11px] text-wheat">
                Claim data syncing… outcome and payout are not shown until the read succeeds.
              </p>
            )}
            {claim && (
              <div className="mt-4 grid gap-3 sm:grid-cols-4">
                <Cell k="Outcome" v={claim.outcome} cls={claim.payout_tier > 0 ? TIERS[claim.payout_tier].color : ""} />
                <Cell k="Validator agreement" v={`${claim.source_count}/3 sources`} />
                <Cell k="Median deficit" v={`${claim.median_deficit}%`} />
                <Cell k="Approved" v={`${fromAtto(approved, 4)} GEN`} />
                <p className="font-mono text-[11px] text-wheat sm:col-span-4">{outcomeNote(claim.outcome, claim.source_count, claim.settlement_mode)}</p>
                {(claim.revisions ?? 0) > 0 && (
                  <p className="font-mono text-[11px] text-cyan sm:col-span-4">
                    re-priced {claim.revisions}× inside the cooldown after upstream observations were revised (max {MAX_REVISIONS})
                  </p>
                )}
                {reserve > 0n && (
                  <p className="font-mono text-[11px] text-mist sm:col-span-4">
                    {fromAtto(reserve, 4)} GEN of coverage is still held back so a revision stays fundable. It is released
                    when the cooldown ends.
                  </p>
                )}
                <p className="font-mono text-[11px] text-mist sm:col-span-4">{claim.reasoning}</p>
                {claim.payout_tier >= 0 && claim.settlement_mode !== "BOUNDARY_CONSERVATIVE" && conservativeTier(claim.median_deficit) < claim.payout_tier && (
                  <p className="font-mono text-[11px] text-mist sm:col-span-4">
                    deficit {claim.median_deficit}% sits within 5 points of a tier threshold: validators on either side
                    converge on the lower tier, and the guaranteed lower bound would be tier {conservativeTier(claim.median_deficit)}.
                  </p>
                )}
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
              {policy.status === "ACTIVE" && matured && inGrace && (
                <button
                  className="btn"
                  disabled={!!busy || !chainOk}
                  onClick={() => act("resolve_boundary_dispute", policy.policy_id)}
                  title={`Policyholder-only fallback. Settles at the guaranteed lower bound: the tier of (deficit - 5 points), so a claim stuck at a tier threshold always settles.`}
                >
                  {busy === policy.policy_id + "resolve_boundary_dispute"
                    ? "Resolving…"
                    : "Settle at guaranteed lower bound"}
                </button>
              )}
              {approved > 0n && revision === "open" && (
                <button
                  className="btn"
                  disabled={!!busy || !chainOk}
                  onClick={() => act("re_evaluate_revised_claim", policy.policy_id)}
                  title="Re-reads all three sources. The payout moves only if the observations were materially revised."
                >
                  {busy === policy.policy_id + "re_evaluate_revised_claim" ? "Re-reading sources…" : "Re-check revised observations"}
                </button>
              )}
              {approved > 0n && revision === "wait" && (
                <span className="font-mono text-xs text-mist">revision check available shortly · cooldown {fmt(cooldown)} left</span>
              )}
              {approved > 0n && revision === "limit" && (
                <span className="font-mono text-xs text-mist">revision limit reached</span>
              )}
              {finalizable && (
                <button
                  className="btn"
                  disabled={!!busy || !chainOk}
                  onClick={() => act("finalize_claim", policy.policy_id)}
                  title="Releases the coverage held back for revisions to the pool"
                >
                  {busy === policy.policy_id + "finalize_claim" ? "Finalizing…" : "Finalize & release reserve"}
                </button>
              )}
              {policy.status === "ACTIVE" && now > policy.end_ts + EXPIRY_GRACE_DAYS * 86400 && (
                <>
                  <span className="font-mono text-xs text-rust">evaluation window closed · reserve can now be released</span>
                  <button
                    className="btn"
                    disabled={!!busy || !chainOk}
                    onClick={() => act("expire_policy", policy.policy_id)}
                  >
                    {busy === policy.policy_id + "expire_policy" ? "Releasing…" : "Release Expired Capital"}
                  </button>
                </>
              )}
              {policy.status === "SETTLED_NO_PAYOUT" && (
                <span className="font-mono text-xs text-mist">settled · no payout · final</span>
              )}
              {approved > 0n && (
                <button
                  className="btn btn-solid"
                  disabled={!!busy || !chainOk || cooldown > 0 || releasableNow === 0n}
                  onClick={() => act("release_payout", policy.policy_id)}
                >
                  Release {fromAtto(releasableNow, 4)} GEN
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
