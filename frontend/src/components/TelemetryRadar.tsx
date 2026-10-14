"use client";
import { useCallback, useEffect, useState } from "react";
import { api, GENESIS_PLOTS, Plot, tierForDeficit, TIERS, windowBaselineMm } from "@/lib/contract";

interface Reading {
  name: string;
  status: "idle" | "ok" | "fail";
  ms?: number;
  windowMm?: number[];
  tier?: number;
}

const iso = (d: Date) => d.toISOString().slice(0, 10);
const cmp = (d: Date) => iso(d).replaceAll("-", "");

// Same shape as the contract: 2 consecutive 14-day windows, newest day lagged 6d.
function span() {
  const end = new Date();
  end.setUTCHours(0, 0, 0, 0);
  end.setUTCDate(end.getUTCDate() - 6);
  const start = new Date(end);
  start.setUTCDate(start.getUTCDate() - 27);
  return { start, end };
}

function windowsFrom(days: { date: string; mm: number }[], start: Date) {
  const out = [0, 0];
  const t0 = start.getTime();
  for (const d of days) {
    const idx = Math.floor((new Date(d.date + "T00:00:00Z").getTime() - t0) / 86400000);
    if (idx >= 0 && idx < 28) out[idx < 14 ? 0 : 1] += d.mm;
  }
  return out;
}

async function timed<T>(fn: () => Promise<T>): Promise<{ v: T; ms: number }> {
  const t = performance.now();
  const ctl = new AbortController();
  const to = setTimeout(() => ctl.abort(), 9000);
  try {
    const v = await fn();
    return { v, ms: Math.round(performance.now() - t) };
  } finally {
    clearTimeout(to);
  }
}

export default function TelemetryRadar({ plotId }: { plotId: string | null }) {
  const [plot, setPlot] = useState<Plot | null>(GENESIS_PLOTS[0]);
  const [rows, setRows] = useState<Reading[]>([
    { name: "Open-Meteo ERA5", status: "idle" },
    { name: "NOAA GFS (Open-Meteo)", status: "idle" },
    { name: "NASA POWER", status: "idle" },
  ]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.plots().then((r) => setPlot(r.data.find((p) => p.plot_id === plotId) ?? r.data[0] ?? null));
  }, [plotId]);

  const scan = useCallback(async () => {
    if (!plot) return;
    setBusy(true);
    const { start, end } = span();
    const lat = plot.latitude.toFixed(4);
    const lon = plot.longitude.toFixed(4);
    const om = (host: string, path: string, extra = "") =>
      `https://${host}${path}?latitude=${lat}&longitude=${lon}&start_date=${iso(start)}&end_date=${iso(end)}&daily=precipitation_sum&timezone=UTC${extra}`;
    // Each window is judged against the baseline of the month its midpoint falls in.
    const baseTenths = [0, 1].map((w) => {
      const mid = new Date(start.getTime() + (w * 14 + 6.5) * 86400000);
      return Math.round(windowBaselineMm(plot, mid.getUTCMonth() + 1) * 10);
    });

    const toRow = (name: string, windows: number[], ms: number): Reading => {
      const defs = windows.map((w, i) => {
        const t = Math.round(w * 10);
        const b = baseTenths[i];
        return b <= 0 || t >= b ? 0 : Math.floor(((b - t) * 100) / b);
      });
      return { name, status: "ok", ms, windowMm: windows, tier: tierForDeficit(Math.min(...defs)) };
    };
    const omFetch = async (name: string, url: string): Promise<Reading> => {
      try {
        const { v, ms } = await timed(async () => {
          const r = await fetch(url);
          if (!r.ok) throw new Error(String(r.status));
          return r.json();
        });
        const days = (v.daily.time as string[]).map((date, i) => ({ date, mm: v.daily.precipitation_sum[i] ?? 0 }));
        return toRow(name, windowsFrom(days, start), ms);
      } catch {
        return { name, status: "fail" };
      }
    };
    const nasa = async (): Promise<Reading> => {
      const name = "NASA POWER";
      try {
        const { v, ms } = await timed(async () => {
          const u = `https://power.larc.nasa.gov/api/temporal/daily/point?parameters=PRECTOTCORR&community=AG&longitude=${lon}&latitude=${lat}&start=${cmp(start)}&end=${cmp(end)}&format=JSON`;
          const r = await fetch(u);
          if (!r.ok) throw new Error(String(r.status));
          return r.json();
        });
        const series = v.properties.parameter.PRECTOTCORR as Record<string, number>;
        const days = Object.entries(series).map(([k, mm]) => ({
          date: `${k.slice(0, 4)}-${k.slice(4, 6)}-${k.slice(6, 8)}`,
          mm: mm < 0 ? 0 : mm,
        }));
        return toRow(name, windowsFrom(days, start), ms);
      } catch {
        return { name, status: "fail" };
      }
    };
    const results = await Promise.all([
      omFetch("Open-Meteo ERA5", om("archive-api.open-meteo.com", "/v1/archive")),
      omFetch("NOAA GFS (Open-Meteo)", om("api.open-meteo.com", "/v1/forecast", "&models=gfs_seamless")),
      nasa(),
    ]);
    setRows(results);
    setBusy(false);
  }, [plot]);

  useEffect(() => {
    scan();
  }, [scan]);

  const valid = rows.filter((r) => r.status === "ok");
  const tiers = new Set(valid.map((r) => r.tier));
  const quorum = valid.length >= 2 && tiers.size === 1;
  const verdict = valid.length < 2 ? "INCONCLUSIVE · too few sources" : tiers.size > 1 ? "INCONCLUSIVE · sources disagree" : TIERS[[...tiers][0] ?? 0].label;

  return (
    <section className="grid gap-6 lg:grid-cols-3">
      <div className="card p-5 lg:col-span-2">
        <div className="flex items-center justify-between">
          <div>
            <div className="label">Live telemetry · {plot?.name ?? "…"}</div>
            <p className="mt-1 text-xs text-mist">Last 28 observed days · 2 × 14-day windows · browser-side preview of what validators fetch.</p>
          </div>
          <button className="btn" onClick={scan} disabled={busy || !plot}>
            {busy ? "Pinging…" : "Re-scan"}
          </button>
        </div>
        <div className="mt-5 space-y-3">
          {rows.map((r) => (
            <div key={r.name} className="flex items-center gap-4 rounded-lg border border-line bg-loam p-3">
              <span
                className={`h-2.5 w-2.5 rounded-full ${r.status === "ok" ? "bg-sprout" : r.status === "fail" ? "bg-rust" : "animate-pulse bg-mist"}`}
              />
              <div className="flex-1">
                <div className="text-sm">{r.name}</div>
                <div className="font-mono text-[11px] text-mist">
                  {r.status === "ok"
                    ? `windows ${r.windowMm!.map((w) => w.toFixed(1)).join(" / ")} mm · ${r.ms} ms`
                    : r.status === "fail"
                      ? "no valid response"
                      : "waiting"}
                </div>
              </div>
              {r.status === "ok" && (
                <span className={`font-mono text-xs ${TIERS[r.tier!].color}`}>TIER {r.tier}</span>
              )}
            </div>
          ))}
        </div>
      </div>

      <div className="card flex flex-col justify-between p-5">
        <div>
          <div className="label">Consensus quorum</div>
          <div className="relative mx-auto mt-4 h-36 w-36 overflow-hidden rounded-full border border-cyan/30 bg-loam">
            {[33, 66].map((r) => (
              <div key={r} className="absolute inset-0 m-auto rounded-full border border-cyan/15" style={{ width: `${r}%`, height: `${r}%` }} />
            ))}
            <div className="radar-sweep absolute inset-0 rounded-full" style={{ background: "conic-gradient(from 0deg, rgba(34,211,238,0.35), transparent 25%)" }} />
            <div className="absolute inset-0 flex flex-col items-center justify-center">
              <span className={`font-display text-3xl font-semibold ${quorum ? "text-sprout" : "text-wheat"}`}>{valid.length}/3</span>
              <span className="font-mono text-[10px] text-mist">sources</span>
            </div>
          </div>
          <div className="mt-4 text-center">
            <div className="label">Agreed tier</div>
            <div className={`mt-1 font-display text-2xl font-semibold ${quorum ? TIERS[[...tiers][0] ?? 0].color : "text-mist"}`}>
              {quorum ? `TIER ${[...tiers][0]}` : "—"}
            </div>
            <p className="mt-1 font-mono text-xs">{verdict}</p>
          </div>
        </div>
        <ul className="mt-6 space-y-1 text-xs text-mist">
          <li>≥ 2 sources must answer HTTP 200</li>
          <li>All answering sources must share one tier</li>
          <li>Both windows must breach (min-of-windows)</li>
          <li>Otherwise: INCONCLUSIVE, zero payout</li>
        </ul>
      </div>
    </section>
  );
}
