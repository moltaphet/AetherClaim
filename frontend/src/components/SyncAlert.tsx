/** Shown instead of zeros or empty lists when an on-chain read did not succeed. */
export default function SyncAlert({ what }: { what: string }) {
  return (
    <div
      role="status"
      className="flex items-start gap-3 rounded-lg border border-wheat/40 bg-wheat/5 px-3 py-2 text-xs text-wheat"
    >
      <span aria-hidden className="mt-1 h-2 w-2 shrink-0 animate-pulse rounded-full bg-wheat" />
      <span>
        Network syncing… Reconnecting to Studio Next RPC. {what} is unavailable until the read succeeds, so no
        balance or count is shown.
      </span>
    </div>
  );
}
