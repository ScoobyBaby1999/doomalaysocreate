// ═══════════════════════════════════════════════════
// OVERLAY PANEL — Settings + Chatbot (collapsible)
// ═══════════════════════════════════════════════════

let panelOpen = false;
let panelCollapsed = false;
let lastPanelView = "settings";

// ── Panel open/close/collapse ─────────────────────

function togglePanel() {
  if (panelOpen) {
    closePanel();
  } else {
    openPanel();
  }
}

function openPanel() {
  openSettingsPanel();
}

function openSettingsPanel() {
  panelOpen = true;
  panelCollapsed = false;

  document.getElementById("overlayPanel").classList.add("panel-open");
  document.getElementById("overlayPanel").classList.remove("panel-collapsed");
  document.getElementById("overlayBackdrop").classList.add("backdrop-visible");
  document.getElementById("overlayStrip").style.display = "none";
  document.getElementById("overlayContent").style.display = "flex";

  document.getElementById("settingsView").style.display = "";
  document.getElementById("chatView").style.display = "none";
  document.getElementById("calendarView").style.display = "none";
  document.getElementById("outputsView").style.display = "none";
  document.getElementById("logView").style.display = "none";
  document.getElementById("overlayTitle").textContent = "Settings";
  lastPanelView = "settings";

  loadThemeState();
  loadApiKeys();
}

function openChatPanel() {
  panelOpen = true;
  panelCollapsed = false;

  document.getElementById("overlayPanel").classList.add("panel-open");
  document.getElementById("overlayPanel").classList.remove("panel-collapsed");
  document.getElementById("overlayBackdrop").classList.add("backdrop-visible");
  document.getElementById("overlayStrip").style.display = "none";
  document.getElementById("overlayContent").style.display = "flex";

  document.getElementById("settingsView").style.display = "none";
  document.getElementById("calendarView").style.display = "none";
  document.getElementById("chatView").style.display = "";
  document.getElementById("calendarView").style.display = "none";
  document.getElementById("outputsView").style.display = "none";
  document.getElementById("logView").style.display = "none";
  document.getElementById("overlayTitle").textContent = "Chat";
  lastPanelView = "chat";

  syncTasksFromServer();
  updateChatTaskLabel();
}

function openCalendarPanel() {
  panelOpen = true;
  panelCollapsed = false;

  document.getElementById("overlayPanel").classList.add("panel-open");
  document.getElementById("overlayPanel").classList.remove("panel-collapsed");
  document.getElementById("overlayBackdrop").classList.add("backdrop-visible");
  document.getElementById("overlayStrip").style.display = "none";
  document.getElementById("overlayContent").style.display = "flex";

  document.getElementById("settingsView").style.display = "none";
  document.getElementById("chatView").style.display = "none";
  document.getElementById("calendarView").style.display = "";
  document.getElementById("outputsView").style.display = "none";
  document.getElementById("logView").style.display = "none";
  document.getElementById("overlayTitle").textContent = "Calendar";
  lastPanelView = "calendar";

  generateCalendar();
}

function openOutputsPanel() {
  panelOpen = true;
  panelCollapsed = false;

  document.getElementById("overlayPanel").classList.add("panel-open");
  document.getElementById("overlayPanel").classList.remove("panel-collapsed");
  document.getElementById("overlayBackdrop").classList.add("backdrop-visible");
  document.getElementById("overlayStrip").style.display = "none";
  document.getElementById("overlayContent").style.display = "flex";

  document.getElementById("settingsView").style.display = "none";
  document.getElementById("chatView").style.display = "none";
  document.getElementById("calendarView").style.display = "none";
  document.getElementById("logView").style.display = "none";
  document.getElementById("outputsView").style.display = "";
  document.getElementById("overlayTitle").textContent = "Outputs";
  lastPanelView = "outputs";

  loadOutputsList();
}

function openLogPanel() {
  panelOpen = true;
  panelCollapsed = false;

  document.getElementById("overlayPanel").classList.add("panel-open");
  document.getElementById("overlayPanel").classList.remove("panel-collapsed");
  document.getElementById("overlayBackdrop").classList.add("backdrop-visible");
  document.getElementById("overlayStrip").style.display = "none";
  document.getElementById("overlayContent").style.display = "flex";

  document.getElementById("settingsView").style.display = "none";
  document.getElementById("chatView").style.display = "none";
  document.getElementById("calendarView").style.display = "none";
  document.getElementById("outputsView").style.display = "none";
  document.getElementById("logView").style.display = "";
  document.getElementById("overlayTitle").textContent = "Log";
  lastPanelView = "log";

  startLogPolling();
}

function closePanel() {
  panelOpen = false;
  panelCollapsed = false;

  document.getElementById("overlayPanel").classList.remove("panel-open");
  document.getElementById("overlayPanel").classList.remove("panel-collapsed");
  document.getElementById("overlayBackdrop").classList.remove("backdrop-visible");
  document.getElementById("overlayStrip").style.display = "none";
  document.getElementById("overlayContent").style.display = "none";

  stopLogPolling();
}

function collapsePanel() {
  if (!panelOpen) return;

  panelCollapsed = true;
  panelOpen = false;

  document.getElementById("overlayPanel").classList.remove("panel-open");
  document.getElementById("overlayPanel").classList.add("panel-collapsed");
  document.getElementById("overlayBackdrop").classList.remove("backdrop-visible");
  document.getElementById("overlayStrip").style.display = "flex";
  document.getElementById("overlayContent").style.display = "none";

  stopLogPolling();
}

function expandPanel() {
  if (panelCollapsed) {
    switch (lastPanelView) {
      case "chat":     openChatPanel(); break;
      case "calendar": openCalendarPanel(); break;
      case "outputs":  openOutputsPanel(); break;
      case "log":      openLogPanel(); break;
      default:         openSettingsPanel(); break;
    }
  }
}

// ── Theme toggle ──────────────────────────────────

function loadThemeState() {
  const saved = localStorage.getItem("theme") || "dark";
  const isLight = saved === "light";
  const toggle = document.getElementById("themeToggle");
  const label = document.getElementById("themeLabel");

  if (isLight) {
    document.documentElement.setAttribute("data-theme", "light");
    toggle.checked = true;
    label.textContent = "Light";
  } else {
    document.documentElement.removeAttribute("data-theme");
    toggle.checked = false;
    label.textContent = "Dark";
  }
}

document.addEventListener("DOMContentLoaded", function() {
  loadThemeState();

  const toggle = document.getElementById("themeToggle");
  if (toggle) {
    toggle.addEventListener("change", function() {
      setTheme(this.checked ? "light" : "dark");
    });
  }
});

function setTheme(theme) {
  if (theme === "light") {
    document.documentElement.setAttribute("data-theme", "light");
    localStorage.setItem("theme", "light");
    document.getElementById("themeLabel").textContent = "Light";
  } else {
    document.documentElement.removeAttribute("data-theme");
    localStorage.setItem("theme", "dark");
    document.getElementById("themeLabel").textContent = "Dark";
  }
}

// ── API Key management ────────────────────────────

async function loadApiKeys() {
  try {
    const res = await fetch("/api/settings/keys");
    if (!res.ok) return;
    const data = await res.json();

    const providers = ["openrouter", "groq", "cerebras", "nvidia"];
    providers.forEach(p => {
      const input = document.getElementById("key-" + p);
      const status = document.getElementById("status-" + p);
      if (!input || !status) return;

      if (data[p] && data[p].configured) {
        input.value = data[p].masked;
        input.placeholder = data[p].masked;
        status.innerHTML = '<span class="status-dot status-configured"></span>';
      } else {
        input.value = "";
        input.placeholder = "Not configured";
        status.innerHTML = '<span class="status-dot status-missing"></span>';
      }
    });
  } catch (e) {
    console.warn("Failed to load API keys:", e);
  }
}

async function saveApiKey(provider) {
  const input = document.getElementById("key-" + provider);
  const status = document.getElementById("status-" + provider);
  const key = input.value.trim();

  if (!key || key.includes("********")) {
    input.style.borderColor = "var(--danger)";
    setTimeout(() => { input.style.borderColor = ""; }, 2000);
    return;
  }

  status.innerHTML = '<span class="status-dot status-saving"></span>';

  try {
    const res = await fetch("/api/settings/keys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider, key }),
    });

    const data = await res.json();

    if (!res.ok) {
      status.innerHTML = '<span class="status-dot status-error" title="' + (data.error || "Save failed") + '"></span>';
      input.style.borderColor = "var(--danger)";
      setTimeout(() => { input.style.borderColor = ""; }, 3000);
      return;
    }

    input.value = data.masked;
    input.placeholder = data.masked;
    status.innerHTML = '<span class="status-dot status-configured"></span>';
    input.style.borderColor = "var(--success)";
    setTimeout(() => { input.style.borderColor = ""; }, 3000);
  } catch (e) {
    status.innerHTML = '<span class="status-dot status-error"></span>';
    console.warn("Failed to save API key:", e);
  }
}

function toggleKeyVisibility(provider) {
  const input = document.getElementById("key-" + provider);
  if (!input) return;
  input.type = input.type === "password" ? "text" : "password";
}
