export default function WalletSimNote() {
  return (
    <p className="mt-3 flex gap-2 rounded-lg border border-line bg-loam/60 px-3 py-2 text-[11px] leading-relaxed text-mist">
      <span aria-hidden className="text-cyan">ⓘ</span>
      <span>
        Note: If MetaMask shows a simulation warning on Studio Next, proceed with confirmation. GenLayer
        transactions settle via GenVM consensus and official protocol fee estimates.
      </span>
    </p>
  );
}
