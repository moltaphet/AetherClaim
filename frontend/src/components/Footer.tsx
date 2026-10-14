"use client";
import { CONTRACT_ADDRESS, EXPLORER } from "@/lib/contract";

const GITHUB = "https://github.com/moltaphet/AetherClaim";

const NAV = [
  { id: "plots", label: "Plot Registry" },
  { id: "telemetry", label: "Telemetry Radar" },
  { id: "vault", label: "LP Vault" },
  { id: "claims", label: "Claims" },
  { id: "about", label: "About" },
] as const;

export default function Footer({ onNavigate }: { onNavigate: (id: string) => void }) {
  const contractUrl = `${EXPLORER}/address/${CONTRACT_ADDRESS}`;
  const short = `${CONTRACT_ADDRESS.slice(0, 8)}…${CONTRACT_ADDRESS.slice(-6)}`;

  return (
    <footer className="mt-20 border-t border-line pt-10">
      {/* Top: brand + quick nav */}
      <div className="flex flex-col gap-8 md:flex-row md:items-start md:justify-between">
        <div className="max-w-sm">
          <div className="font-display text-2xl tracking-tight">
            Aether<span className="bg-gradient-to-r from-sprout to-cyan bg-clip-text text-transparent">Claim</span>
          </div>
          <p className="mt-2 text-sm leading-relaxed text-mist">
            Autonomous parametric climate insurance powered by GenLayer GenVM
          </p>
        </div>
        <nav aria-label="Footer" className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
          {NAV.map((n) => (
            <a
              key={n.id}
              href={`#${n.id}`}
              onClick={(e) => {
                e.preventDefault();
                onNavigate(n.id);
              }}
              className="text-mist transition hover:text-cyan"
            >
              {n.label}
            </a>
          ))}
        </nav>
      </div>

      {/* Middle: network and contract chips */}
      <div className="mt-8 flex flex-wrap items-center gap-3">
        <a
          href={contractUrl}
          target="_blank"
          rel="noreferrer"
          title={CONTRACT_ADDRESS}
          className="inline-flex items-center gap-2 rounded-full border border-cyan/40 bg-cyan/5 px-3 py-1.5 font-mono text-xs text-cyan transition hover:bg-cyan/10"
        >
          <span className="text-mist">Contract</span>
          <span className="sm:hidden">{short}</span>
          <span className="hidden sm:inline">{CONTRACT_ADDRESS}</span>
          <span aria-hidden>↗</span>
        </a>
        <span className="inline-flex items-center gap-2 rounded-full border border-sprout/40 bg-sprout/5 px-3 py-1.5 font-mono text-xs text-sprout">
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-sprout opacity-60" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-sprout" />
          </span>
          GenLayer Studio Next (Chain ID: 61997)
        </span>
        <a
          href={GITHUB}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-2 rounded-full border border-line px-3 py-1.5 font-mono text-xs text-white transition hover:border-white/40"
        >
          <svg className="h-4 w-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
            <path d="M12 .5a11.5 11.5 0 0 0-3.64 22.41c.58.1.79-.25.79-.56v-2c-3.2.7-3.88-1.37-3.88-1.37-.52-1.33-1.28-1.69-1.28-1.69-1.05-.72.08-.7.08-.7 1.15.08 1.76 1.19 1.76 1.19 1.03 1.76 2.7 1.25 3.36.96.1-.75.4-1.25.73-1.54-2.55-.29-5.24-1.28-5.24-5.7 0-1.26.45-2.29 1.19-3.1-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.18 1.18a11 11 0 0 1 5.78 0c2.2-1.49 3.17-1.18 3.17-1.18.63 1.59.23 2.76.11 3.05.74.81 1.19 1.84 1.19 3.1 0 4.43-2.7 5.4-5.27 5.69.41.36.78 1.06.78 2.14v3.17c0 .31.21.67.8.56A11.5 11.5 0 0 0 12 .5Z" />
          </svg>
          GitHub
        </a>
      </div>

      {/* Bottom: disclaimer + copyright */}
      <div className="mt-8 border-t border-line pt-6 text-xs leading-relaxed text-mist">
        <p>
          AetherClaim is a trust-minimized parametric prototype built on GenLayer Studio Next. For demonstration and
          hackathon evaluation purposes.
        </p>
        <p className="mt-2 font-mono text-[11px]">© 2026 AetherClaim · prototype build, October 2026 · not audited, no real funds.</p>
      </div>
    </footer>
  );
}
