"use client";
import { useSyncExternalStore } from "react";
import { readHealth } from "@/lib/contract";

export default function SyncBadge() {
  const h = useSyncExternalStore(readHealth.subscribe, readHealth.get, () => "unknown" as const);
  if (h === "live")
    return (
      <span className="inline-flex items-center gap-2 rounded-full border border-sprout/40 px-3 py-1 font-mono text-[11px] text-sprout">
        <span className="h-1.5 w-1.5 rounded-full bg-sprout" /> live chain state
      </span>
    );
  if (h === "cached")
    return (
      <span className="inline-flex items-center gap-2 rounded-full border border-wheat/30 px-3 py-1 font-mono text-[11px] text-wheat/80">
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-wheat" />
        Live network read syncing; displaying cached chain state
      </span>
    );
  return (
    <span className="inline-flex items-center gap-2 rounded-full border border-line px-3 py-1 font-mono text-[11px] text-mist">
      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-mist" /> connecting…
    </span>
  );
}
