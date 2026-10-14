"use client";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { CHAIN_ID_HEX, ensureChain, injected } from "./contract";

interface WalletCtx {
  account: string | null;
  chainOk: boolean;
  connecting: boolean;
  error: string | null;
  connect: () => Promise<void>;
  disconnect: () => void;
}

const Ctx = createContext<WalletCtx | null>(null);
const KEY = "aether.disconnected";

export function WalletProvider({ children }: { children: React.ReactNode }) {
  const [account, setAccount] = useState<string | null>(null);
  const [chainOk, setChainOk] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const syncChain = useCallback(async () => {
    const p = injected();
    if (!p) return;
    const id = (await p.request({ method: "eth_chainId" })) as string;
    setChainOk(id?.toLowerCase() === CHAIN_ID_HEX);
  }, []);

  const connect = useCallback(async () => {
    const p = injected();
    if (!p) {
      setError("No EIP-1193 wallet found. Install MetaMask.");
      return;
    }
    setConnecting(true);
    setError(null);
    try {
      const accs = (await p.request({ method: "eth_requestAccounts" })) as string[];
      await ensureChain(p); // network auto-prompt
      localStorage.removeItem(KEY);
      setAccount(accs[0] ?? null);
      await syncChain();
    } catch (e: any) {
      setError(e?.message ?? "Connection rejected");
    } finally {
      setConnecting(false);
    }
  }, [syncChain]);

  const disconnect = useCallback(() => {
    localStorage.setItem(KEY, "1");
    setAccount(null);
  }, []);

  useEffect(() => {
    const p = injected();
    if (!p) return;
    if (!localStorage.getItem(KEY)) {
      p.request({ method: "eth_accounts" })
        .then((a) => {
          const list = a as string[];
          if (list[0]) setAccount(list[0]);
        })
        .catch(() => undefined);
    }
    syncChain().catch(() => undefined);
    const onAccounts = (a: string[]) => setAccount(localStorage.getItem(KEY) ? null : (a[0] ?? null));
    const onChain = () => syncChain().catch(() => undefined);
    p.on?.("accountsChanged", onAccounts);
    p.on?.("chainChanged", onChain);
    return () => {
      p.removeListener?.("accountsChanged", onAccounts);
      p.removeListener?.("chainChanged", onChain);
    };
  }, [syncChain]);

  const value = useMemo(
    () => ({ account, chainOk, connecting, error, connect, disconnect }),
    [account, chainOk, connecting, error, connect, disconnect],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useWallet(): WalletCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("WalletProvider missing");
  return v;
}
