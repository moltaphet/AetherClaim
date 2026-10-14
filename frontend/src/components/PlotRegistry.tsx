"use client";
import { useEffect, useState } from "react";
import { api, Plot } from "@/lib/contract";

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
  const [plots, setPlots] = useState<Plot[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api
      .plots()
      .then(setPlots)
      .catch((e) => setErr(e.message));
  }, []);

  const sel = plots.find((p) => p.plot_id === selected) ?? plots[0];

  return (
    <section className="grid gap-6 lg:grid-cols-5">
      <div className="card relative overflow-hidden p-4 lg:col-span-3">
        <div className="label mb-3">Authenticated parcel map</div>
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
        {err && <p className="mt-3 text-sm text-rust">Registry unavailable: {err}</p>}
        {!sel && !err && <p className="mt-3 text-sm text-mist">Loading registry…</p>}
        {sel && (
          <div className="mt-3 space-y-3">
            <h3 className="font-display text-2xl">{sel.name}</h3>
            <dl className="grid grid-cols-2 gap-3 text-sm">
              <Field k="Plot ID" v={sel.plot_id} mono />
              <Field k="Crop" v={`${CROP_GLYPH[sel.crop_type] ?? "◇"} ${sel.crop_type}`} />
              <Field k="Latitude" v={sel.latitude.toFixed(4)} mono />
              <Field k="Longitude" v={sel.longitude.toFixed(4)} mono />
              <Field k="Baseline / 14d" v={`${sel.baseline_rainfall_mm} mm`} mono />
              <Field k="Crop cycle" v={sel.crop_cycle} />
            </dl>
            <div>
              <div className="label">Cadastral boundary hash</div>
              <code className="mt-1 block break-all rounded bg-loam p-2 font-mono text-[11px] text-mist">
                {sel.cadastral_hash}
              </code>
            </div>
            <span
              className={`inline-block rounded-full border px-3 py-1 font-mono text-xs ${sel.active ? "border-sprout text-sprout" : "border-wheat text-wheat"}`}
            >
              {sel.active ? "ACTIVE · insurable" : "STAGED · 24h delay"}
            </span>
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
              {p.latitude.toFixed(2)}, {p.longitude.toFixed(2)} · {p.baseline_rainfall_mm} mm/14d
            </div>
          </button>
        ))}
      </div>
    </section>
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
