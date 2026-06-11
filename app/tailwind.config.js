/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0b0d10", surface: "#14181d", surface2: "#1b2026",
        border: "#262d36", text: "#e6e9ee", muted: "#8b95a3", accent: "#5b8cff",
      },
      fontFamily: { mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"] },
    },
  },
  plugins: [],
};
