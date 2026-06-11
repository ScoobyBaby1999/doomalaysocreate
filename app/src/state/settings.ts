import { useEffect, useState } from "react";
import type { Settings } from "../api/panel";

const KEY = "loom.settings.v1";

// In dev (browser) default to the Vite proxy at /backend. In a Tauri build, the user
// sets the real gateway URL in Settings (and the WebView can call it cross-origin).
const DEFAULTS: Settings = {
  baseUrl: "/backend",
  token: "",
};

export function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return { ...DEFAULTS, ...JSON.parse(raw) };
  } catch {
    /* ignore */
  }
  return DEFAULTS;
}

export function saveSettings(s: Settings) {
  localStorage.setItem(KEY, JSON.stringify(s));
}

/** Simple settings hook with persistence; the token never leaves the device. */
export function useSettings(): [Settings, (s: Settings) => void] {
  const [settings, setSettings] = useState<Settings>(loadSettings);
  useEffect(() => saveSettings(settings), [settings]);
  return [settings, setSettings];
}
