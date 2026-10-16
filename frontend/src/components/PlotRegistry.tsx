"use client";
import { useEffect, useState } from "react";
import { api, GENESIS_PLOTS, fromAtto, MIN_INSURABLE_BASELINE_MM, monthInCycle, MONTHS, Plot, Policy, STAGING_DELAY_DAYS, windowInsurable } from "@/lib/contract";
import SyncBadge from "./SyncBadge";
import { useWallet } from "@/lib/useAether";
import { coverageLock, eligibility, WEATHER_SOURCES } from "@/lib/underwriting";

const CROP_GLYPH: Record<string, string> = { ALMOND: "◐", CORN: "▥", OLIVE: "◍" };

// Equirectangular projection onto a small world panel.
function project(lat: number, lon: number) {
  return { x: ((lon + 180) / 360) * 100, y: ((90 - lat) / 180) * 100 };
}

export default function PlotRegistry({
  selected,
  onSelect,
}: {
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  // Never blank: start from the pre-seeded genesis registry, upgrade to live data.
  const [plots, setPlots] = useState<Plot[]>(GENESIS_PLOTS);
  const [policies, setPolicies] = useState<Policy[] | null>(null);
  const [live, setLive] = useState(false);
  const { account } = useWallet();
  const nowSec = Math.floor(Date.now() / 1000);

  useEffect(() => {
    api.plots().then((r) => {
      setPlots(r.data);
      setLive(r.live);
    });
    api.policies("").then((r) => setPolicies(r.live ? r.data : null));
  }, []);

  const coverage = (id: string) => {
    if (!policies) return null;
    const mine = policies.filter((p) => p.plot_id === id && p.status === "ACTIVE");
    const total = mine.reduce((a, p) => a + BigInt(p.coverage_amount), 0n);
    return { n: mine.length, total };
  };

  const sel = plots.find((p) => p.plot_id === selected) ?? plots[0];

  return (
    <section className="grid gap-6 lg:grid-cols-5">
      <div className="card relative overflow-hidden p-4 lg:col-span-3">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2"><div className="label">Authenticated parcel map</div><SyncBadge /></div>
        <div className="relative aspect-[2/1] rounded-lg border border-line bg-loam">
          {[25, 50, 75].map((p) => (
            <div key={p} className="absolute inset-x-0 border-t border-line/60" style={{ top: `${p}%` }} />
          ))}
          {[25, 50, 75].map((p) => (
            <div key={p} className="absolute inset-y-0 border-l border-line/60" style={{ left: `${p}%` }} />
          ))}
          {plots.map((p) => {
            const { x, y } = project(p.latitude, p.longitude);
            const on = sel?.plot_id === p.plot_id;
            return (
              <button
                key={p.plot_id}
                onClick={() => onSelect(p.plot_id)}
                style={{ left: `${x}%`, top: `${y}%` }}
                className="group absolute -translate-x-1/2 -translate-y-1/2"
                title={p.name}
              >
                <span
                  className={`block h-3.5 w-3.5 rounded-full border-2 ${on ? "border-wheat bg-wheat shadow-[0_0_18px_#e9c46a]" : "border-sprout bg-sprout/40"}`}
                />
                <span className="absolute left-5 top-1/2 hidden -translate-y-1/2 whitespace-nowrap rounded bg-soil px-2 py-1 font-mono text-[10px] group-hover:block">
                  {p.plot_id}
                </span>
              </button>
            );
          })}
        </div>
        <p className="mt-3 text-xs text-mist">
          Coordinates are immutable registry entries. Observation URLs are built inside the contract from these
          values — no caller-supplied coordinate ever reaches a weather fetch.
        </p>
      </div>

      <div className="card p-5 lg:col-span-2">
        <div className="label">Parcel inspector</div>
        {sel && (
          <div className="mt-3 space-y-3">
            <h3 className="font-display text-2xl">{sel.name}</h3>
            <dl className="grid grid-cols-2 gap-3 text-sm">
              <Field k="Plot ID" v={sel.plot_id} mono />
              <Field k="Crop" v={`${CROP_GLYPH[sel.crop_type] ?? "◇"} ${sel.crop_type}`} />
              <Field k="Latitude" v={sel.latitude.toFixed(4)} mono />
              <Field k="Longitude" v={sel.longitude.toFixed(4)} mono />
              <Field k="Annual baseline" v={`${sel.monthly_baselines.reduce((a, b) => a + b, 0)} mm`} mono />
              <Field k="Insurable cycle" v={sel.crop_cycle} />
            </dl>
            <div>
              <div className="label">Cadastral boundary hash</div>
              <code className="mt-1 block break-all rounded bg-loam p-2 font-mono text-[11px] text-mist">
                {sel.cadastral_hash}
              </code>
            </div>
            <div>
              <div className="label">Monthly rainfall baseline (mm) · lit = insurable</div>
              <div className="mt-2 flex h-24 items-end gap-1">
                {sel.monthly_baselines.map((mm, i) => {
                  const max = Math.max(...sel.monthly_baselines, 1);
                  const inCycle = monthInCycle(i + 1, sel.crop_cycle_start_month, sel.crop_cycle_end_month);
                  const on = inCycle && windowInsurable(sel, i + 1);
                  return (
                    <div key={i} className="flex flex-1 flex-col items-center gap-1" title={`${MONTHS[i]}: ${mm} mm`}>
                      <div
                        className={`w-full rounded-t ${on ? "bg-gradient-to-t from-cyan to-sprout" : "bg-line"}`}
                        style={{ height: `${Math.max(4, (mm / max) * 100)}%` }}
                      />
                      <span className={`font-mono text-[9px] ${on ? "text-cyan" : "text-mist/60"}`}>{MONTHS[i][0]}</span>
                    </div>
                  );
                })}
              </div>
              <p className="mt-1 font-mono text-[11px] text-mist">
                Each 14-day window is judged against its own month. Off-season months, and windows expecting under {MIN_INSURABLE_BASELINE_MM} mm, cannot pay.
              </p>
            </div>
            <Provenance plot={sel} live={live} account={account} nowSec={nowSec} />
            <div className="flex flex-wrap gap-2">
              <span className={`rounded-full border px-3 py-1 font-mono text-xs ${sel.active ? "border-sprout text-sprout" : "border-wheat text-wheat"}`}>
                {sel.active ? "ACTIVE · insurable" : `STAGED · ${STAGING_DELAY_DAYS}d delay`}
              </span>
              <span className="rounded-full border border-line px-3 py-1 font-mono text-xs text-mist">
                {(() => {
                  const c = coverage(sel.plot_id);
                  return c ? `${c.n} live ${c.n === 1 ? "policy" : "policies"} · ${fromAtto(c.total, 3)} GEN` : "coverage: syncing";
                })()}
              </span>
            </div>
          </div>
        )}
      </div>

      <div className="grid gap-4 sm:grid-cols-3 lg:col-span-5">
        {plots.map((p) => (
          <button
            key={p.plot_id}
            onClick={() => onSelect(p.plot_id)}
            className={`card p-4 text-left transition hover:border-wheat/60 ${sel?.plot_id === p.plot_id ? "border-wheat/70" : ""}`}
          >
            <div className="label">{p.crop_type}</div>
            <div className="mt-1 font-display text-lg">{p.name}</div>
            <div className="mt-2 font-mono text-xs text-mist">
              {p.latitude.toFixed(2)}, {p.longitude.toFixed(2)} · {p.crop_cycle}
            </div>
          </button>
        ))}
      </div>
    </section>
  );
}

function Provenance({ plot, live, account, nowSec }: { plot: Plot; live: boolean; account: string | null; nowSec: number }) {
  // Provenance, eligibility and locks are on-chain facts. When the read did not
  // succeed they are shown as unknown, never as verified.
  const verified = live ? !!plot.provenance_verified : null;
  const elig = live ? eligibility(plot, account) : "unknown";
  const lock = coverageLock(live ? plot : undefined, nowSec);
  const tag = (ok: boolean | null, yes: string, no: string, unknown: string) =>
    ok === null ? (
      <span className="rounded-full border border-line px-3 py-1 font-mono text-xs text-mist">{unknown}</span>
    ) : ok ? (
      <span className="rounded-full border border-sprout px-3 py-1 font-mono text-xs text-sprout">{yes}</span>
    ) : (
      <span className="rounded-full border border-rust px-3 py-1 font-mono text-xs text-rust">{no}</span>
    );
  return (
    <div className="space-y-3 rounded-lg border border-line p-3">
      <div className="label">Underwriting status</div>
      <div className="flex flex-wrap gap-2">
        {tag(verified, "CADASTRAL ATTESTATION VERIFIED", "NO VALID CADASTRAL ATTESTATION · not insurable", "attestation · syncing")}
        {tag(
          elig === "unknown" ? null : elig === "eligible",
          "ELIGIBLE · you are the operator",
          "NOT ELIGIBLE · operator only",
          account ? "eligibility · syncing" : "eligibility · connect wallet",
        )}
        {tag(live ? !lock.locked : null, "COVERAGE OPEN", `COVERAGE LOCKED · until ${new Date(lock.until * 1000).toISOString().slice(0, 10)}`, "coverage · syncing")}
      </div>
      <dl className="grid grid-cols-2 gap-3 text-xs">
        <Field k="Registered operator" v={plot.eligible_policyholder ? `${plot.eligible_policyholder.slice(0, 8)}…${plot.eligible_policyholder.slice(-6)}` : "—"} mono />
        <Field k="Plot coverage cap" v={plot.max_plot_coverage ? `${fromAtto(plot.max_plot_coverage, 2)} GEN` : "—"} mono />
        <Field k="Unresolved on plot" v={plot.active_coverage !== undefined ? `${fromAtto(plot.active_coverage, 3)} GEN` : "—"} mono />
        <Field k="Gauge station (NOAA)" v={plot.ghcnd_station ?? "—"} mono />
      </dl>
      {plot.registry_record_id && (
        <div>
          <div className="label">Cadastral authority record · Ed25519 signature checked on-chain</div>
          <code className="mt-1 block break-all rounded bg-loam p-2 font-mono text-[10px] text-mist">
            {plot.registry_record_id} · {plot.attestation_signature?.slice(0, 32)}…
          </code>
        </div>
      )}
      {plot.provenance_manifest_sha256 && (
        <div>
          <div className="label">Manifest digest validators read</div>
          <code className="mt-1 block break-all rounded bg-loam p-2 font-mono text-[10px] text-mist">{plot.provenance_manifest_sha256}</code>
        </div>
      )}
      <div>
        <div className="label">Three independent weather hosts</div>
        <ul className="mt-1 space-y-1 font-mono text-[11px] text-mist">
          {WEATHER_SOURCES.map((s) => (
            <li key={s.key}>
              <span className="text-cyan">{s.host}</span> · {s.operator}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function Field({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div>
      <dt className="label">{k}</dt>
      <dd className={mono ? "font-mono" : ""}>{v}</dd>
    </div>
  );
}
