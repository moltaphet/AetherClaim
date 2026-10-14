import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        soil: "#09090b",
        furrow: "#0f1318",
        loam: "#161b22",
        line: "#273140",
        wheat: "#fbbf24",
        sprout: "#34d399",
        cyan: { DEFAULT: "#22d3ee" },
        rust: "#fb7185",
        mist: "#94a3b8",
      },
      fontFamily: {
        display: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
export default config;
