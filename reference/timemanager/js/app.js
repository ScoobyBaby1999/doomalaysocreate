// ═══════════════════════════════════════════════════
// APP INITIALIZATION — runs on page load
// ═══════════════════════════════════════════════════

// WHAT DOES THIS FILE DO?
// It sets up the dashboard when the page loads:
// 1. Checks if the user is logged in (redirect to login if not)
// 2. Loads tasks from the server (with localStorage as fallback)
// 3. Wires up all the button click handlers
// 4. Initializes the calendar and template dropdown

// If the user is not logged in, send them to the login page
if (!localStorage.getItem("user")) {
  window.location.href = "login.html";
}

window.onload = async function () {
  // ── Step 1: Try to load tasks from the server ───
  //
  // WHY DO WE DO THIS?
  // The server is the source of truth for tasks. This ensures
  // that tasks created by the chatbot are visible here.
  // If the server request fails (offline, server down), we fall
  // back to loading from localStorage so the app still works.

  const currentUserEmail = localStorage.getItem("user") || "";

  try {
    const res = await fetch("/api/tasks", {
      headers: { "X-User-Email": currentUserEmail },
    });

    if (res.ok) {
      const data = await res.json();
      // Server tasks become our local notes array
      notes = data.tasks || [];
      // Also update localStorage as a cache for offline use
      saveNotes();
    } else {
      // Server returned an error — fall back to localStorage
      loadNotes();
    }
  } catch (e) {
    // Server is unreachable — fall back to localStorage
    console.warn("Could not load tasks from server, using local cache:", e);
    loadNotes();
  }

  // ── Step 2: Select the first task (if any exist) ─

  if (notes.length > 0) {
    activeId = notes[0].id;
  }

  // ── Step 3: Render the UI ───────────────────────

  renderList();
  loadTask();
  generateCalendar();
  scanTemplates();
  updateChatTaskLabel();

  // ── Step 4: Wire up button handlers ─────────────

  document.getElementById("addBtn").onclick = addTask;
  document.getElementById("saveBtn").onclick = saveTask;
  document.getElementById("deleteBtn").onclick = deleteTask;
  document.getElementById("processBtn").onclick = processTask;

  document.getElementById("logBtn").onclick = () => openLogPanel();
  document.getElementById("calendarBtn").onclick = () => openCalendarPanel();
  document.getElementById("helpBtn").onclick = () => openChatPanel();
  document.getElementById("outputsBtn").onclick = () => openOutputsPanel();

  document.getElementById("logoutBtn").onclick = logout;
  document.getElementById("settingsBtn").onclick = () => openSettingsPanel();
};
