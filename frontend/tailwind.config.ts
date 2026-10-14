import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        soil: "#0b0d0a",
        furrow: "#12160f",
        loam: "#1a2015",
        line: "#2a3322",
        wheat: "#e9c46a",
        sprout: "#8fd16a",
        rust: "#e76f51",
        mist: "#9aa592",
      },
      fontFamily: {
        display: ["Fraunces", "Georgia", "serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
export default config;
