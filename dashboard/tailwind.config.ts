import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg0: "#0A0E14",
        bg1: "#0F141B",
        bg2: "#161C25",
        border: "#1F2937",
        text0: "#E6EDF3",
        text1: "#9DA7B3",
        text2: "#5C6773",
        accent: "#00D9C0",
        bull: "#26D9A8",
        bear: "#F0616D",
        warn: "#F0B429",
        halt: "#FF3D5A",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      fontSize: {
        // Base 12px, table 11px, headers tiny + uppercase.
        xs: ["11px", { lineHeight: "14px" }],
        sm: ["12px", { lineHeight: "16px" }],
        base: ["13px", { lineHeight: "18px" }],
        lg: ["15px", { lineHeight: "20px" }],
        xl: ["18px", { lineHeight: "24px" }],
        "2xl": ["22px", { lineHeight: "28px" }],
        "3xl": ["28px", { lineHeight: "32px" }],
      },
      keyframes: {
        flash: {
          "0%": { backgroundColor: "rgba(38, 217, 168, 0.25)" },
          "100%": { backgroundColor: "transparent" },
        },
        flashDown: {
          "0%": { backgroundColor: "rgba(240, 97, 109, 0.25)" },
          "100%": { backgroundColor: "transparent" },
        },
        shimmer: {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(100%)" },
        },
      },
      animation: {
        flash: "flash 200ms ease-out",
        "flash-down": "flashDown 200ms ease-out",
        shimmer: "shimmer 1.5s linear infinite",
      },
    },
  },
  plugins: [],
};

export default config;
