"use client";
import { useState } from "react";
import ClaimStream from "@/components/ClaimStream";
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
              Aether<span className="text-wheat">Claim</span>
            </h1>
            <p className="text-xs text-mist">Parametric climate &amp; agricultural insurance · GenLayer</p>
          </div>
          <WalletManager />
        </header>

        <nav className="mb-8 flex gap-1 overflow-x-auto border-b border-line">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`whitespace-nowrap px-4 py-3 text-sm transition ${tab === t.id ? "border-b-2 border-wheat text-wheat" : "text-mist hover:text-white"}`}
            >
              {t.label}
            </button>
          ))}
        </nav>

        {tab === "plots" && <PlotRegistry selected={plot} onSelect={setPlot} />}
        {tab === "telemetry" && <TelemetryRadar plotId={plot} />}
        {tab === "desk" && <PolicyVault plotId={plot} onChange={() => setTick((n) => n + 1)} />}
        {tab === "claims" && <ClaimStream refreshKey={tick} />}

        <footer className="mt-16 font-mono text-[11px] text-mist">
          contract{" "}
          <a className="text-wheat underline" href={`${EXPLORER}/address/${CONTRACT_ADDRESS}`} target="_blank" rel="noreferrer">
            {CONTRACT_ADDRESS}
          </a>{" "}
          · chain 61997
        </footer>
      </div>
    </WalletProvider>
  );
}
