/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      // Add an `xs:` breakpoint for very-narrow phones (portrait < 640).
      // Default Tailwind only ships sm/md/lg/xl/2xl. We use `xs:` to hide
      // low-priority header chrome on the smallest screens.
      screens: {
        xs: "480px",
      },
      colors: {
        // Purple/black theme — near-black background, dark purple surfaces,
        // vivid purple primary, with a lighter accent for highlights.
        bg: "#0a0a0a",          // near-black background
        surface: "#1a1024",      // very dark purple surface
        surface2: "#241433",     // slightly lighter dark purple
        surface3: "#2e1a40",     // hover/elevated surface
        border: "#3b0764",       // dark purple border
        borderHover: "#5b21b6",  // lighter purple border on hover
        text: "#f3f4f6",         // near-white text
        muted: "#8b95a3",        // muted gray (used for both text-muted and bg-muted/NN)
        "muted-foreground": "#a1a1aa",  // shadcn-style muted text (slightly lighter gray)
        accent: "#a855f7",       // purple-500 — primary
        accentHover: "#9333ea",  // purple-600 — primary hover
        accentDeep: "#7e22ce",   // purple-700 — pressed/active
        accentLight: "#c084fc",  // purple-400 — highlights
        accentSoft: "#d8b4fe",   // purple-300 — soft text on purple
        // shadcn-style names (back the `bg-primary`, `text-foreground`, etc.
        // classes used by ModelSelectOverlay). Hardcoded hex so Tailwind's
        // `/NN` opacity modifier works correctly.
        background: "#0a0a0a",
        foreground: "#f3f4f6",
        primary: "#a855f7",
        "primary-foreground": "#ffffff",
        ring: "#a855f7",
        destructive: "#f43f5e",
      },
      fontFamily: { mono: ["ui-monospace", "SFMono-Regular", "Men", "monospace"] },
      borderRadius: {
        // Encourage rounder corners by default for chat elements.
        xl2: "1rem",
        xl3: "1.5rem",
      },
    },
  },
  plugins: [],
};
