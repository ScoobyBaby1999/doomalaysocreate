function showPage(page) {
  document.getElementById("tasksPage").style.display = "none";

  document.getElementById(page + "Page").style.display = "flex";
}

async function processTask() {
  const t = notes.find(n => n.id === activeId);
  if (!t) {
    showProcessStatus("No task selected.", "error");
    return;
  }

  const title = document.getElementById("noteTitle").value.trim();
  const body = document.getElementById("noteContent").value.trim();
  const priority = document.getElementById("priority").value;
  const template = document.getElementById("template").value;
  const date = document.getElementById("date").value;

  if (!title) {
    showProcessStatus("Task must have a title.", "error");
    return;
  }
  if (!body) {
    showProcessStatus("Task must have a description.", "error");
    return;
  }

  const statusEl = document.getElementById("processStatus");
  const btn = document.getElementById("processBtn");

  btn.disabled = true;
  btn.textContent = "Processing...";
  showProcessStatus("Sending to backend orchestrator...", "info");

  try {
    const res = await fetch("/api/process", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title,
        body,
        priority,
        template,
        date,
      }),
    });

    const data = await res.json();

    if (!res.ok) {
      showProcessStatus(`Error: ${data.error}`, "error");
      return;
    }

    if (data.status === "done") {
      showProcessStatus(
        `Done: ${data.word_count} words written.${data.output_file ? ` Output: ${data.output_file}` : ""}`,
        "success"
      );
    } else if (data.status === "failed") {
      showProcessStatus(`Failed: ${data.error}`, "error");
    } else if (data.status === "paused") {
      showProcessStatus(`Paused (network issue): ${data.error}`, "warning");
    } else {
      showProcessStatus(`Status: ${data.status}`, "info");
    }

    // Save task after successful processing
    t.title = title;
    t.content = body;
    t.priority = priority;
    t.template = template;
    t.date = date;
    saveNotes();
    renderList();
  } catch (e) {
    showProcessStatus(`Request failed: ${e.message}`, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Process (AI)";
  }
}

function showProcessStatus(message, type) {
  const el = document.getElementById("processStatus");
  el.style.display = "block";
  el.textContent = message;
  el.className = `process-status process-${type}`;
}
