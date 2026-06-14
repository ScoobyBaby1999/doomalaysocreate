import { useEffect, useState } from "react";
import type { Settings } from "../api/panel";

const KEY = "loom.settings.v1";
const EXPIRY_KEY = "loom.settings.expiry";
const EXPIRY_MS = 7 * 24 * 60 * 60 * 1000; // 7 days

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
    // Check credential expiry — clear stale credentials from shared/public computers
    const expiry = localStorage.getItem(EXPIRY_KEY);
    if (expiry && Date.now() > Number(expiry)) {
      localStorage.removeItem(KEY);
      localStorage.removeItem(EXPIRY_KEY);
      return DEFAULTS;
    }
    const raw = localStorage.getItem(KEY);
    if (raw) return { ...DEFAULTS, ...JSON.parse(raw) };
  } catch {
    /* ignore */
  }
  return DEFAULTS;
}

export function saveSettings(s: Settings) {
  localStorage.setItem(KEY, JSON.stringify(s));
  // Set/refresh expiry whenever credentials are saved
  if (s.token || s.rotationSecret) {
    localStorage.setItem(EXPIRY_KEY, String(Date.now() + EXPIRY_MS));
  }
}

/** Simple settings hook with persistence; the token never leaves the device. */
export function useSettings(): [Settings, (s: Settings) => void] {
  const [settings, setSettings] = useState<Settings>(loadSettings);
  useEffect(() => saveSettings(settings), [settings]);
  return [settings, setSettings];
}
