import type { ITheme } from "@xterm/xterm";

/**
 * Terminal palettes matched to the panel's dark and light themes. xterm needs
 * concrete colours (no CSS variables), so these mirror the card surfaces in
 * index.css. The light palette darkens yellow/cyan/white so they stay legible.
 */
export const TERMINAL_THEMES: Record<"dark" | "light", ITheme> = {
  dark: {
    background: "#15181d",
    foreground: "#d6dbe1",
    cursor: "#15181d",
    cursorAccent: "#15181d",
    selectionBackground: "#3b4656",
    selectionInactiveBackground: "#2c3440",
    scrollbarSliderBackground: "rgba(255,255,255,0.12)",
    scrollbarSliderHoverBackground: "rgba(255,255,255,0.2)",
    scrollbarSliderActiveBackground: "rgba(255,255,255,0.28)",
    overviewRulerBorder: "#15181d",
    black: "#2a2f37",
    red: "#f2777a",
    green: "#5fd49a",
    yellow: "#f0c36b",
    blue: "#7aa6f0",
    magenta: "#c79bf2",
    cyan: "#5fc7d4",
    white: "#c8cdd4",
    brightBlack: "#7d8590",
    brightRed: "#ff9a9c",
    brightGreen: "#86e8b6",
    brightYellow: "#ffd98a",
    brightBlue: "#9cc0ff",
    brightMagenta: "#dcb8ff",
    brightCyan: "#8ee3ec",
    brightWhite: "#f2f4f7",
  },
  light: {
    background: "#fbfbfc",
    foreground: "#1f2328",
    cursor: "#fbfbfc",
    cursorAccent: "#fbfbfc",
    selectionBackground: "#c8d7ee",
    selectionInactiveBackground: "#dde5f0",
    scrollbarSliderBackground: "rgba(0,0,0,0.14)",
    scrollbarSliderHoverBackground: "rgba(0,0,0,0.22)",
    scrollbarSliderActiveBackground: "rgba(0,0,0,0.3)",
    overviewRulerBorder: "#fbfbfc",
    black: "#1f2328",
    red: "#c62828",
    green: "#1a7f37",
    yellow: "#9a6700",
    blue: "#0b5cad",
    magenta: "#8250df",
    cyan: "#0a7480",
    white: "#57606a",
    brightBlack: "#6e7781",
    brightRed: "#d1242f",
    brightGreen: "#2da44e",
    brightYellow: "#b07c00",
    brightBlue: "#218bff",
    brightMagenta: "#a475f9",
    brightCyan: "#1b8a96",
    brightWhite: "#24292f",
  },
};

/** Search highlight colours (#RRGGBB required by the search addon). */
export const SEARCH_DECORATIONS = {
  dark: {
    matchBackground: "#4a4a20",
    matchBorder: "#8a8a3a",
    matchOverviewRuler: "#c9b458",
    activeMatchBackground: "#8a6d1c",
    activeMatchBorder: "#f0c36b",
    activeMatchColorOverviewRuler: "#f0c36b",
  },
  light: {
    matchBackground: "#fff2b3",
    matchBorder: "#e0c050",
    matchOverviewRuler: "#c9a227",
    activeMatchBackground: "#ffd34d",
    activeMatchBorder: "#b07c00",
    activeMatchColorOverviewRuler: "#b07c00",
  },
};
