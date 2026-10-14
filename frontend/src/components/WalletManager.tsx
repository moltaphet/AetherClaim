"use client";
import { useWallet } from "@/lib/useAether";
import { CHAIN_ID } from "@/lib/contract";

export default function WalletManager() {
  const { account, chainOk, connecting, error, connect, disconnect } = useWallet();
  const short = account ? `${account.slice(0, 6)}…${account.slice(-4)}` : "";
  return (
    <div className="flex items-center gap-3">
      {account && (
        <span
          className={`hidden items-center gap-2 font-mono text-xs sm:flex ${chainOk ? "text-sprout" : "text-rust"}`}
        >
          <span className={`h-2 w-2 rounded-full ${chainOk ? "bg-sprout" : "bg-rust"}`} />
          {chainOk ? `chain ${CHAIN_ID}` : "wrong network"}
        </span>
      )}
      {account ? (
        <>
          <span className="rounded-lg border border-line bg-loam px-3 py-2 font-mono text-xs">{short}</span>
          <button className="btn" onClick={disconnect}>
            Disconnect
          </button>
        </>
      ) : (
        <button className="btn btn-solid" onClick={connect} disabled={connecting}>
          {connecting ? "Connecting…" : "Connect wallet"}
        </button>
      )}
      {error && <span className="max-w-[16rem] truncate text-xs text-rust" title={error}>{error}</span>}
    </div>
  );
}
