"use client";
import { useCallback, useEffect, useState } from "react";
import { WEATHER_SOURCES } from "@/lib/underwriting";
import {
  api,
  computeDeficitTier,
  GENESIS_PLOTS,
  Plot,
  tenthsFromMm,
  TIERS,
  windowBaselineTenths,
  windowDeficit,
} from "@/lib/contract";

type Status = "idle" | "ok" | "offline" | "data_error";

interface Reading {
  name: string;
  status: Status;
  ms?: number;
  windowMm?: number[];
  tier?: number;
  note?: string;
}

/** Hard ceiling for every third-party weather request. */
const FETCH_TIMEOUT_MS = 6000;

/** The station answered, but the payload is unusable. Not the same as offline. */
class DataError extends Error {}

const iso = (d: Date) => d.toISOString().slice(0, 10);
const cmp = (d: Date) => iso(d).replaceAll("-", "");

// Same shape as the contract: 2 consecutive 14-day windows. The newest observed
// day is lagged by the 10-day stabilisation period, so preliminary values have settled.
function span() {
  const end = new Date();
  end.setUTCHours(0, 0, 0, 0);
  end.setUTCDate(end.getUTCDate() - 11);
  const start = new Date(end);
  start.setUTCDate(start.getUTCDate() - 27);
  return { start, end };
}

/**
 * Sum the 28 days into two windows, in tenths of a millimetre, rounding per day
 * the way the contract does. Every day must be present and be a finite,
 * non-negative number. A missing, null, NaN, negative or non-numeric day makes
 * the whole station a DataError. It is never coerced to 0, because 0 mm reads as
 * a catastrophic drought.
 */
function strictWindows(byDate: Map<string, unknown>, start: Date): number[] {
  const out = [0, 0];
  for (let i = 0; i < 28; i++) {
    const d = new Date(start.getTime() + i * 86400000);
    const key = iso(d);
    const v = byDate.get(key);
    if (typeof v !== "number" || !Number.isFinite(v) || v < 0) {
      throw new DataError(`${key}: ${v === undefined ? "missing" : v === null ? "null" : `invalid (${String(v)})`}`);
    }
    out[i < 14 ? 0 : 1] += tenthsFromMm(v); // banker's rounding, same as the contract
  }
  return out;
}

async function fetchJson(url: string): Promise<{ json: any; ms: number }> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), FETCH_TIMEOUT_MS);
  const t0 = performance.now();
  try {
    const r = await fetch(url, { signal: ctl.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const json = await r.json();
    return { json, ms: Math.round(performance.now() - t0) };
  } finally {
    clearTimeout(timer);
  }
}

const label: Record<Status, string> = {
  idle: "waiting",
  ok: "ok",
  offline: "OFFLINE",
  data_error: "DATA_ERROR",
};

export default function TelemetryRadar({ plotId }: { plotId: string | null }) {
  const [plot, setPlot] = useState<Plot | null>(GENESIS_PLOTS[0]);
  const [rows, setRows] = useState<Reading[]>([
    { name: `ERA5 · ${WEATHER_SOURCES[0].host}`, status: "idle" },
    { name: `NASA POWER · ${WEATHER_SOURCES[1].host}`, status: "idle" },
    { name: `NOAA NCEI gauge · ${WEATHER_SOURCES[2].host}`, status: "idle" },
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
      return windowBaselineTenths(plot, mid.getUTCMonth() + 1);
    });

    const toRow = (name: string, windowsTenths: number[], ms: number): Reading => {
      // Same arithmetic as the contract: integer floor division, floor for arid windows.
      const defs = windowsTenths.map((t, i) => windowDeficit(baseTenths[i], t));
      return {
        name,
        status: "ok",
        ms,
        windowMm: windowsTenths.map((t) => t / 10),
        tier: computeDeficitTier(Math.min(...defs)),
      };
    };

    const classify = (name: string, e: unknown): Reading =>
      e instanceof DataError
        ? { name, status: "data_error", note: e.message }
        : { name, status: "offline", note: e instanceof Error && e.name === "AbortError" ? "timed out after 6s" : "no response" };

    const omFetch = async (name: string, url: string): Promise<Reading> => {
      try {
        const { json, ms } = await fetchJson(url);
        const times = json?.daily?.time;
        const vals = json?.daily?.precipitation_sum;
        if (!Array.isArray(times) || !Array.isArray(vals)) throw new DataError("unexpected response shape");
        const byDate = new Map<string, unknown>(times.map((t: string, i: number) => [t, vals[i]]));
        return toRow(name, strictWindows(byDate, start), ms);
      } catch (e) {
        return classify(name, e);
      }
    };
    const station = plot.ghcnd_station;
    const ncei = async (): Promise<Reading> => {
      const name = `NOAA NCEI gauge · ${WEATHER_SOURCES[2].host}`;
      try {
        if (!station) throw new DataError("plot has no GHCN-Daily station");
        const u = `https://${WEATHER_SOURCES[2].host}/access/services/data/v1?dataset=daily-summaries&dataTypes=PRCP&stations=${station}&startDate=${iso(start)}&endDate=${iso(end)}&format=json&units=metric`;
        const { json, ms } = await fetchJson(u);
        if (!Array.isArray(json)) throw new DataError("unexpected response shape");
        // A day with no GHCN-Daily row is a missing observation, never a dry day.
        const byDate = new Map<string, unknown>();
        for (const row of json as Array<Record<string, unknown>>) {
          const n = typeof row.PRCP === "string" && row.PRCP !== "" ? Number(row.PRCP) : NaN;
          if (typeof row.DATE === "string") byDate.set(row.DATE, Number.isFinite(n) ? n : null);
        }
        return toRow(name, strictWindows(byDate, start), ms);
      } catch (e) {
        return classify(name, e);
      }
    };
    const nasa = async (): Promise<Reading> => {
      const name = `NASA POWER · ${WEATHER_SOURCES[1].host}`;
      try {
        const u = `https://power.larc.nasa.gov/api/temporal/daily/point?parameters=PRECTOTCORR&community=AG&longitude=${lon}&latitude=${lat}&start=${cmp(start)}&end=${cmp(end)}&format=JSON`;
        const { json, ms } = await fetchJson(u);
        const series = json?.properties?.parameter?.PRECTOTCORR;
        if (!series || typeof series !== "object") throw new DataError("unexpected response shape");
        // NASA POWER marks a missing day as -999. strictWindows rejects it.
        const byDate = new Map<string, unknown>(
          Object.entries(series as Record<string, unknown>).map(([k, v]) => [`${k.slice(0, 4)}-${k.slice(4, 6)}-${k.slice(6, 8)}`, v]),
        );
        return toRow(name, strictWindows(byDate, start), ms);
      } catch (e) {
        return classify(name, e);
      }
    };
    const results = await Promise.all([
      omFetch(`ERA5 · ${WEATHER_SOURCES[0].host}`, om(WEATHER_SOURCES[0].host, "/v1/archive")),
      nasa(),
      ncei(),
    ]);
    setRows(results);
    setBusy(false);
  }, [plot]);

  useEffect(() => {
    scan();
  }, [scan]);

  // Only stations with a complete, valid reading take part in the consensus view.
  const valid = rows.filter((r) => r.status === "ok");
  const excluded = rows.filter((r) => r.status === "offline" || r.status === "data_error");
  // Same aggregation as the contract: median of three answering sources, or the
  // lower of two. Fewer than two is a quorum failure, which allows a retry.
  const sorted = valid.map((r) => r.tier!).sort((a, b) => a - b);
  const quorum = valid.length >= 2;
  const agreed = quorum ? (valid.length === 3 ? sorted[1] : sorted[0]) : null;
  const split = quorum && new Set(sorted).size > 1;
  const verdict = !quorum
    ? "QUORUM_FAILURE · retry permitted"
    : `${valid.length === 3 ? "median of 3 (anyone may settle)" : "lower of 2 (policyholder only)"} → ${TIERS[agreed!].label}${split ? " · sources split" : ""}`;

  return (
    <section className="grid gap-6 lg:grid-cols-3">
      <div className="card p-5 lg:col-span-2">
        <div className="flex items-center justify-between">
          <div>
            <div className="label">Live telemetry · {plot?.name ?? "…"}</div>
            <p className="mt-1 text-xs text-mist">Live preview: two 14-day windows ending 11 days ago, three operators on three hosts. A policy settles only on its own term&apos;s final 28 days, fixed at purchase.</p>
          </div>
          <button className="btn" onClick={scan} disabled={busy || !plot}>
            {busy ? "Pinging…" : "Re-scan"}
          </button>
        </div>
        <div className="mt-5 space-y-3">
          {rows.map((r) => (
            <div key={r.name} className="flex items-center gap-4 rounded-lg border border-line bg-loam p-3">
              <span
                className={`h-2.5 w-2.5 rounded-full ${
                  r.status === "ok"
                    ? "bg-sprout"
                    : r.status === "offline"
                      ? "bg-rust"
                      : r.status === "data_error"
                        ? "bg-wheat"
                        : "animate-pulse bg-mist"
                }`}
              />
              <div className="flex-1">
                <div className="text-sm">{r.name}</div>
                <div className="font-mono text-[11px] text-mist">
                  {r.status === "ok"
                    ? `windows ${r.windowMm!.map((w) => w.toFixed(1)).join(" / ")} mm · ${r.ms} ms`
                    : r.status === "idle"
                      ? "waiting"
                      : `${label[r.status]} · ${r.note ?? ""} · excluded from consensus`}
                </div>
              </div>
              {r.status === "ok" && (
                <span className={`font-mono text-xs ${TIERS[r.tier!].color}`}>TIER {r.tier}</span>
              )}
              {(r.status === "offline" || r.status === "data_error") && (
                <span className={`font-mono text-xs ${r.status === "offline" ? "text-rust" : "text-wheat"}`}>{label[r.status]}</span>
              )}
            </div>
          ))}
        </div>
        {excluded.length > 0 && (
          <p className="mt-3 text-xs text-mist">
            {excluded.length} station{excluded.length > 1 ? "s" : ""} excluded. An unusable reading is never treated as
            0 mm, which would falsely read as a catastrophic drought.
          </p>
        )}
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
            <div className={`mt-1 font-display text-2xl font-semibold ${quorum ? TIERS[agreed!].color : "text-mist"}`}>
              {quorum ? `TIER ${agreed}` : "—"}
            </div>
            <p className="mt-1 font-mono text-xs">{verdict}</p>
          </div>
        </div>
        <ul className="mt-6 space-y-1 text-xs text-mist">
          <li>≥ 2 sources must answer HTTP 200 with every day valid, else QUORUM_FAILURE (retry)</li>
          <li>Three answer: median tier, any caller may settle. Two answer: lower tier, only the policyholder may accept</li>
          <li>A split never pays zero. One faulty station cannot erase a claim two sources support</li>
          <li>Each source uses the lower of its two windows (sustained deviation)</li>
        </ul>
      </div>
    </section>
  );
}
