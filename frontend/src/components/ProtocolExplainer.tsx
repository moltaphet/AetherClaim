"use client";
import { useState } from "react";
import { EXPIRY_GRACE_DAYS, MIN_INSURABLE_BASELINE_MM, TIERS } from "@/lib/contract";

const icon = "h-5 w-5";

function MapIcon() {
  return (
    <svg className={icon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden>
      <path d="M9 4 3 6.5v13L9 17l6 3 6-2.5v-13L15 7 9 4Z" strokeLinejoin="round" />
      <path d="M9 4v13M15 7v13" />
    </svg>
  );
}
function RadarIcon() {
  return (
    <svg className={icon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="4.5" />
      <path d="M12 12 18 6" strokeLinecap="round" />
    </svg>
  );
}
function GridIcon() {
  return (
    <svg className={icon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden>
      <rect x="4" y="4" width="7" height="7" rx="1.5" />
      <rect x="13" y="4" width="7" height="7" rx="1.5" />
      <rect x="4" y="13" width="7" height="7" rx="1.5" />
      <rect x="13" y="13" width="7" height="7" rx="1.5" />
    </svg>
  );
}
function VaultIcon() {
  return (
    <svg className={icon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden>
      <rect x="3.5" y="5" width="17" height="14" rx="2" />
      <circle cx="12" cy="12" r="3" />
      <path d="M12 9v-1.5M12 16.5V15M15 12h1.5M7.5 12H9" strokeLinecap="round" />
    </svg>
  );
}

function Badge({ children, tone = "cyan" }: { children: React.ReactNode; tone?: "cyan" | "sprout" | "wheat" }) {
  const cls =
    tone === "sprout"
      ? "border-sprout/40 text-sprout"
      : tone === "wheat"
        ? "border-wheat/40 text-wheat"
        : "border-cyan/40 text-cyan";
  return <span className={`rounded-full border px-2.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${cls}`}>{children}</span>;
}

const DEFICIT_RANGE = ["< 25%", "25% – 49%", "50% – 74%", "≥ 75%"];
const SEASON = ["Normal season", "Moderate drought", "Severe drought", "Catastrophic drought"];

interface CardProps {
  id: string;
  n: number;
  title: string;
  icon: React.ReactNode;
  badges: React.ReactNode;
  summary: React.ReactNode;
  detail: React.ReactNode;
  extra?: React.ReactNode;
  open: boolean;
  onToggle: () => void;
}

function FeatureCard({ id, n, title, icon: Icon, badges, summary, detail, extra, open, onToggle }: CardProps) {
  return (
    <article className="card flex flex-col p-5">
      <div className="flex items-start justify-between gap-3">
        <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-cyan/30 bg-cyan/5 text-cyan">{Icon}</span>
        <span className="font-mono text-[11px] text-mist">0{n}</span>
      </div>
      <h3 className="mt-4 text-lg font-semibold leading-snug">{title}</h3>
      <div className="mt-3 flex flex-wrap gap-2">{badges}</div>
      <div className="mt-4 text-sm leading-relaxed text-mist">{summary}</div>
      {extra}
      <button
        type="button"
        className="mt-4 self-start font-mono text-xs text-cyan underline-offset-4 hover:underline"
        aria-expanded={open}
        aria-controls={`${id}-detail`}
        onClick={onToggle}
      >
        {open ? "Hide detail" : "Show detail"}
      </button>
      {open && (
        <div id={`${id}-detail`} className="mt-3 border-t border-line pt-3 text-sm leading-relaxed text-mist">
          {detail}
        </div>
      )}
    </article>
  );
}

export default function ProtocolExplainer() {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const toggle = (id: string) => setOpen((o) => ({ ...o, [id]: !o[id] }));

  return (
    <section id="about" className="scroll-mt-6">
      <div className="max-w-3xl">
        <div className="label">About</div>
        <h2 className="mt-1 text-2xl font-semibold tracking-tight sm:text-3xl">
          How AetherClaim Works: Trust-Minimized Parametric Insurance
        </h2>
        <p className="mt-3 text-sm leading-relaxed text-mist">
          A policy pays out on measured rainfall, not on a claims adjuster&apos;s opinion. Four rules do the work. They are
          enforced by the contract, and each one closes a specific way the product could otherwise be gamed.
        </p>
      </div>

      <div className="mt-8 grid gap-5 md:grid-cols-2">
        <FeatureCard
          id="registry"
          n={1}
          title="Authenticated Plot Registry & Actuarial Floor"
          icon={<MapIcon />}
          badges={
            <>
              <Badge>Immutable registry</Badge>
              <Badge tone="sprout">12 monthly baselines</Badge>
              <Badge tone="wheat">{MIN_INSURABLE_BASELINE_MM} mm floor</Badge>
            </>
          }
          summary={
            <>
              Cover attaches only to a plot that validators have <b className="text-white">verified against a pinned
              cadastral manifest</b> (coordinates, 12 monthly baselines, crop cycle, coverage cap, SHA-256 commitment),
              and only the plot&apos;s registered operator can insure it. A policyholder cannot supply a location or a URL.
            </>
          }
          detail={
            <>
              Each 14-day window is judged against the baseline of its own month. A window expecting under{" "}
              <b className="text-white">{MIN_INSURABLE_BASELINE_MM} mm</b> cannot evidence a drought, so it reads 0%, and the
              contract refuses to sell a term that contains one. One live coverage window per plot, capped by the plot&apos;s
              registered maximum. New plots wait 7 days after staging, and the admin address cannot hold policies.
            </>
          }
          open={!!open.registry}
          onToggle={() => toggle("registry")}
        />

        <FeatureCard
          id="consensus"
          n={2}
          title="3-Source Telemetry Consensus"
          icon={<RadarIcon />}
          badges={
            <>
              <Badge>ERA5 · NASA POWER · NOAA NCEI</Badge>
              <Badge tone="sprout">3 sources: median</Badge>
              <Badge tone="wheat">2 sources: holder only</Badge>
            </>
          }
          summary={
            <>
              Validators independently fetch Open-Meteo ERA5, NASA POWER and a NOAA NCEI rain-gauge station, on three
              different hostnames and operators. With all three answering,{" "}
              <b className="text-white">anyone</b> may settle at the median tier. With two, the lower tier settles only if{" "}
              <b className="text-white">the policyholder</b> accepts it. With fewer than two, the call is a quorum failure
              and can be retried.
            </>
          }
          detail={
            <>
              A third party can never lock the insured into a lower tier during an outage. The contract refuses to run
              if two sources share a hostname or an operator domain. Honest limit: ERA5 and NASA POWER both assimilate
              satellite and station data, so they are independent infrastructure but not independent physics. The NOAA
              gauge is the one ground-truth vote, and it can be tens of kilometres from the plot.
            </>
          }
          open={!!open.consensus}
          onToggle={() => toggle("consensus")}
        />

        <FeatureCard
          id="buckets"
          n={3}
          title="Deterministic Payout Buckets"
          icon={<GridIcon />}
          badges={
            <>
              <Badge>4 discrete tiers</Badge>
              <Badge tone="sprout">Zero financial drift</Badge>
            </>
          }
          summary={
            <>
              Only four payouts exist. Every accepted evaluation moves one of these four amounts, and a boundary
              disagreement is always settled toward the lower tier.
            </>
          }
          extra={
            <div className="mt-4 grid grid-cols-2 gap-2">
              {TIERS.map((t) => (
                <div key={t.tier} className="rounded-lg border border-line bg-loam p-3">
                  <div className="flex items-baseline justify-between">
                    <span className="font-mono text-[11px] text-mist">TIER {t.tier}</span>
                    <span className={`font-mono text-lg font-semibold ${t.color}`}>{t.payoutBps / 100}%</span>
                  </div>
                  <div className="mt-1 font-mono text-[11px] text-white">Deficit {DEFICIT_RANGE[t.tier]}</div>
                  <div className="font-mono text-[10px] text-mist">{SEASON[t.tier]}</div>
                </div>
              ))}
            </div>
          }
          detail={
            <>
              Validators compare the source count exactly and the deficit within 5 points. A leader at a lower tier is
              accepted, a leader above a validator is not, so two validators straddling a threshold converge on the
              lower tier instead of rotating forever. If a claim is still stuck, the policyholder can settle at the
              guaranteed lower bound. During the 24h cooldown a payout can be re-priced, up or down, if the stations
              revise their observations.
            </>
          }
          open={!!open.buckets}
          onToggle={() => toggle("buckets")}
        />

        <FeatureCard
          id="vault"
          n={4}
          title="ERC-4626 LP Vault & Capital Preservation"
          icon={<VaultIcon />}
          badges={
            <>
              <Badge>Share-based</Badge>
              <Badge tone="sprout">European maturity</Badge>
              <Badge tone="wheat">24h + 3d stream</Badge>
            </>
          }
          summary={
            <>
              LPs deposit GEN for shares in an ERC-4626-style vault. Premiums raise the share price and paid claims lower
              it, <b className="text-white">proportionally for every LP</b>. The verdict is taken once, after the policy&apos;s
              final 28 days have matured, and is final.
            </>
          }
          detail={
            <>
              Approved payouts wait a 24-hour verification cooldown, then stream linearly over 3 days. Exits are capped to
              unreserved capital, so no LP can step out ahead of a loss, and the last LP cannot exit while policies are
              live. Evaluation stays open for {EXPIRY_GRACE_DAYS} days after the term so an outage cannot forfeit a
              claim.
            </>
          }
          open={!!open.vault}
          onToggle={() => toggle("vault")}
        />
      </div>
    </section>
  );
}
