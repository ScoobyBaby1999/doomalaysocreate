import { useEffect, useState } from "react";
import type { Settings } from "../api/panel";

const KEY = "loom.settings.v1";

// In dev (browser) default to the Vite proxy at /backend. A production build is
// served by the gateway itself (same origin), so relative paths just work; a
// separately-hosted build (or Tauri WebView) sets the real URL in Settings.
const DEFAULTS: Settings = {
  baseUrl: import.meta.env.DEV ? "/backend" : "",
  token: "",
  rotationSecret: "",
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
