// ═══════════════════════════════════════════════════
// TASKS — render, load, save, add, delete tasks
// ═══════════════════════════════════════════════════

// WHAT DOES THIS FILE DO?
// It handles the task list UI on the left sidebar and the editor.
// When the user creates, edits, or deletes a task, we:
// 1. Update the local notes array (for instant UI feedback)
// 2. Send the change to the server (so it persists on disk)
// 3. Re-render the sidebar and calendar

// ── Render the task list sidebar ──────────────────

function renderList() {
  const list = document.getElementById("noteList");
  list.innerHTML = "";

  notes.forEach(n => {
    const div = document.createElement("div");
    div.className = "note";
    div.setAttribute("data-priority", n.priority || "medium");
    div.innerText = n.title || "Untitled";

    if (n.id === activeId) div.classList.add("active");

    div.onclick = () => {
      activeId = n.id;
      loadTask();
      renderList();
      updateChatTaskLabel();
    };

    list.appendChild(div);
  });
}

// ── Load the selected task into the editor ────────

function loadTask() {
  const t = notes.find(n => n.id === activeId);
  if (!t) return;

  document.getElementById("noteTitle").value = t.title;
  document.getElementById("noteContent").value = t.content;
  document.getElementById("priority").value = t.priority;
  document.getElementById("template").value = t.template || "freeform";
  document.getElementById("date").value = t.date;
}

// ── Save changes to the currently selected task ──

async function saveTask() {
  const t = notes.find(n => n.id === activeId);
  if (!t) return;

  // Step 1: Update the local copy (instant UI feedback)
  t.title = document.getElementById("noteTitle").value;
  t.content = document.getElementById("noteContent").value;
  t.priority = document.getElementById("priority").value;
  t.template = document.getElementById("template").value;
  t.date = document.getElementById("date").value;

  saveNotes();
  renderList();
  generateCalendar();
  updateChatTaskLabel();

  // Step 2: Send the update to the server so it persists
  try {
    const currentUserEmail = localStorage.getItem("user") || "";
    await fetch("/api/tasks/" + t.id, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "X-User-Email": currentUserEmail,
      },
      body: JSON.stringify({
        title: t.title,
        content: t.content,
        priority: t.priority,
        template: t.template,
        date: t.date,
      }),
    });
  } catch (e) {
    console.warn("Failed to sync task update to server:", e);
  }
}

// ── Add a new task ────────────────────────────────

async function addTask() {
  const id = Date.now();
  const today = new Date();
  const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;

  const newTask = {
    id,
    title: "",
    content: "",
    priority: "medium",
    template: "freeform",
    date: todayStr
  };

  // Step 1: Add locally (instant UI feedback)
  notes.push(newTask);
  activeId = id;
  saveNotes();
  renderList();
  loadTask();
  updateChatTaskLabel();

  // Step 2: Send to server to persist
  try {
    const currentUserEmail = localStorage.getItem("user") || "";
    const res = await fetch("/api/tasks", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-User-Email": currentUserEmail,
      },
      body: JSON.stringify({
        title: "Untitled",
        content: "",
        priority: "medium",
        template: "freeform",
        date: todayStr,
      }),
    });

    if (res.ok) {
      const data = await res.json();
      // Replace the local task ID with the server-assigned ID
      // (The server generates its own ID, so we use that as the source of truth)
      if (data.task && data.task.id !== id) {
        const localTask = notes.find(n => n.id === id);
        if (localTask) {
          localTask.id = data.task.id;
          activeId = data.task.id;
          saveNotes();
          renderList();
        }
      }
    }
  } catch (e) {
    console.warn("Failed to sync new task to server:", e);
  }
}

// ── Delete the currently selected task ────────────

async function deleteTask() {
  const taskIdToDelete = activeId;

  // Step 1: Remove locally (instant UI feedback)
  notes = notes.filter(n => n.id !== taskIdToDelete);
  activeId = notes.length ? notes[0].id : null;
  saveNotes();
  renderList();
  if (activeId) loadTask();
  updateChatTaskLabel();

  // Step 2: Tell the server to delete it
  try {
    const currentUserEmail = localStorage.getItem("user") || "";
    await fetch("/api/tasks/" + taskIdToDelete, {
      method: "DELETE",
      headers: {
        "X-User-Email": currentUserEmail,
      },
    });
  } catch (e) {
    console.warn("Failed to sync task delete to server:", e);
  }
}

// ── Scan available templates ──────────────────────

async function scanTemplates() {
  const select = document.getElementById("template");

  try {
    const res = await fetch("/api/templates");
    if (!res.ok) return;
    const data = await res.json();

    select.innerHTML = "";

    data.templates.forEach(t => {
      const opt = document.createElement("option");
      opt.value = t.name;
      opt.textContent = t.name.replace(/_/g, " ");
      select.appendChild(opt);
    });

    if (!select.value) select.value = "freeform";
  } catch (e) {
    console.warn("failed to scan templates:", e);
  }
}
