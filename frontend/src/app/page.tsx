"use client";
import { useState } from "react";
import ClaimStream from "@/components/ClaimStream";
import LiquidityVault from "@/components/LiquidityVault";
import PlotRegistry from "@/components/PlotRegistry";
import PolicyVault from "@/components/PolicyVault";
import TelemetryRadar from "@/components/TelemetryRadar";
import WalletManager from "@/components/WalletManager";
import { CONTRACT_ADDRESS, EXPLORER } from "@/lib/contract";
import { WalletProvider } from "@/lib/useAether";

const TABS = [
  { id: "plots", label: "Plot Registry" },
  { id: "telemetry", label: "Telemetry" },
  { id: "desk", label: "Underwriting Desk" },
  { id: "vault", label: "LP Vault" },
  { id: "claims", label: "Claims & Vesting" },
] as const;

export default function Page() {
  const [tab, setTab] = useState<(typeof TABS)[number]["id"]>("plots");
  const [plot, setPlot] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  return (
    <WalletProvider>
      <div className="mx-auto max-w-6xl px-5 pb-20">
        <header className="flex flex-wrap items-center justify-between gap-4 py-6">
          <div>
            <h1 className="font-display text-3xl tracking-tight">
              Aether<span className="bg-gradient-to-r from-sprout to-cyan bg-clip-text text-transparent">Claim</span>
            </h1>
            <p className="text-xs text-mist">Parametric climate &amp; agricultural insurance · GenLayer</p>
          </div>
          <WalletManager />
        </header>

        <div className="mb-8 grid grid-cols-2 gap-3 sm:grid-cols-4">
          {[
            ["Payout tiers", "0 · 30 · 70 · 100%"],
            ["Sources", "3 independent"],
            ["Cooldown", "24h"],
            ["Vesting", "3d linear"],
          ].map(([k, v]) => (
            <div key={k} className="card px-4 py-3">
              <div className="label">{k}</div>
              <div className="mt-1 font-mono text-sm text-cyan">{v}</div>
            </div>
          ))}
        </div>

        <nav className="mb-8 flex gap-1 overflow-x-auto border-b border-line">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`whitespace-nowrap px-4 py-3 text-sm transition ${tab === t.id ? "border-b-2 border-cyan text-cyan" : "text-mist hover:text-white"}`}
            >
              {t.label}
            </button>
          ))}
        </nav>

        {tab === "plots" && <PlotRegistry selected={plot} onSelect={setPlot} />}
        {tab === "telemetry" && <TelemetryRadar plotId={plot} />}
        {tab === "desk" && <PolicyVault plotId={plot} onChange={() => setTick((n) => n + 1)} />}
        {tab === "vault" && <LiquidityVault refreshKey={tick} />}
        {tab === "claims" && <ClaimStream refreshKey={tick} />}

        <footer className="mt-16 font-mono text-[11px] text-mist">
          contract{" "}
          <a className="text-cyan underline" href={`${EXPLORER}/address/${CONTRACT_ADDRESS}`} target="_blank" rel="noreferrer">
            {CONTRACT_ADDRESS}
          </a>{" "}
          · chain 61997
        </footer>
      </div>
    </WalletProvider>
  );
}
