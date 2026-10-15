"use client";
import { useCallback, useEffect, useState } from "react";
import ClaimStream from "@/components/ClaimStream";
import Footer from "@/components/Footer";
import LiquidityVault from "@/components/LiquidityVault";
import PlotRegistry from "@/components/PlotRegistry";
import PolicyVault from "@/components/PolicyVault";
import ProtocolExplainer from "@/components/ProtocolExplainer";
import TelemetryRadar from "@/components/TelemetryRadar";
import WalletManager from "@/components/WalletManager";
import { useWallet, WalletProvider } from "@/lib/useAether";

const TABS = [
  { id: "plots", label: "Plot Registry" },
  { id: "telemetry", label: "Telemetry" },
  { id: "desk", label: "Underwriting Desk" },
  { id: "vault", label: "LP Vault" },
  { id: "claims", label: "Claims & Vesting" },
] as const;

type TabId = (typeof TABS)[number]["id"];
const isTab = (id: string): id is TabId => TABS.some((t) => t.id === id);

function Dashboard() {
  const { account } = useWallet();
  const [tab, setTab] = useState<TabId>("plots");
  const [plot, setPlot] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  // Footer and deep links address sections by id. Only one panel is mounted at a
  // time, so an anchor must first select its tab, then scroll once it exists.
  const navigate = useCallback((id: string) => {
    if (isTab(id)) setTab(id);
    if (typeof window !== "undefined") window.history.replaceState(null, "", `#${id}`);
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
  }, []);

  useEffect(() => {
    const fromHash = () => {
      const id = window.location.hash.replace("#", "");
      if (id && (isTab(id) || id === "about")) navigate(id);
    };
    fromHash();
    window.addEventListener("hashchange", fromHash);
    return () => window.removeEventListener("hashchange", fromHash);
  }, [navigate]);

  return (
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
            ["Sources", "3 hosts · 3 operators"],
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

        {/* Keyed by account: switching accounts unmounts every panel, so no policy,
            claim, share balance, notice or busy flag from the previous address can
            survive into the new one. */}
        <div key={account ?? "disconnected"} id={tab} className="scroll-mt-6">
          {tab === "plots" && <PlotRegistry selected={plot} onSelect={setPlot} />}
          {tab === "telemetry" && <TelemetryRadar plotId={plot} />}
          {tab === "desk" && <PolicyVault plotId={plot} onChange={() => setTick((n) => n + 1)} />}
          {tab === "vault" && <LiquidityVault refreshKey={tick} />}
          {tab === "claims" && <ClaimStream refreshKey={tick} />}
        </div>

        <div className="mt-20">
          <ProtocolExplainer />
        </div>

        <Footer onNavigate={navigate} />
      </div>
  );
}

export default function Page() {
  return (
    <WalletProvider>
      <Dashboard />
    </WalletProvider>
  );
}
