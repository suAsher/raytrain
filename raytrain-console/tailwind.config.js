/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Neutral-first workbench palette.
        bg: "#0e1116",        // app background (dark, calm)
        panel: "#161b22",     // panels / surfaces
        panel2: "#1c232c",    // raised surfaces / hover
        border: "#283039",
        borderc: "#323b46",
        ink: "#e6edf3",       // primary text
        ink2: "#9aa7b4",      // secondary text
        ink3: "#697585",      // tertiary / muted
        brand: "#3b82f6",     // primary accent (blue)
        // status colors
        running: "#3b82f6",
        queued: "#f59e0b",
        failed: "#ef4444",
        succeeded: "#22c55e",
        cancelled: "#6b7280",
        starting: "#8b5cf6",
      },
      borderRadius: {
        DEFAULT: "6px",
        md: "6px",
        lg: "8px",
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
