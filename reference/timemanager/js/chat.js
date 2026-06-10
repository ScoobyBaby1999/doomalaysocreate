// ═══════════════════════════════════════════════════
// CHAT — freeform LLM chat with tool-use and history
// ═══════════════════════════════════════════════════

// WHAT DOES THIS FILE DO?
// It handles the chat UI: sending messages, displaying responses,
// and showing when the chatbot uses tools (like creating tasks).
//
// HOW DOES IT WORK?
// 1. User types a message and clicks Send
// 2. The message is sent to the server via POST /api/chat
// 3. The server runs the tool-call loop (LLM → parse tools → execute → loop)
// 4. The response comes back with:
//    - "response": the LLM's final answer text
//    - "tool_calls_made": a list of tools that were executed
// 5. We display the tool results first (as system messages),
//    then display the LLM's response

const CHAT_CONTEXT_LIMIT = 120000; // Maximum characters before auto-new-chat

// The conversation history sent to the LLM
// Each message is { role: "user" or "assistant", content: "text" }
let chatMessages = [];

// ── Chat task label ─────────────────────────────────────────

// Updates the displayed task name in the chat toolbar so the user
// always knows which task is being included when the toggle is on.
function updateChatTaskLabel() {
  const el = document.getElementById("chatTaskLabel");
  if (!el) return;
  if (activeId) {
    const task = notes.find(n => n.id === activeId);
    el.textContent = task ? task.title : "No task selected";
  } else {
    el.textContent = "No task selected";
  }
}

// ── Chat input auto-resize ──────────────────────────────────

// WHAT DOES THIS FUNCTION DO?
// Automatically adjusts the height of the chat textarea to fit its content.
// When the user types and the text wraps to a new line, the textarea grows.
// When text is deleted, it shrinks back down.
//
// HOW DOES IT WORK?
// 1. Reset height to "auto" (so shrinking works — can't shrink from a fixed height)
// 2. Measure the natural scrollHeight (how tall the content wants to be)
// 3. Set the height to scrollHeight, capped at 120px
function autoResizeChatInput() {
  const input = document.getElementById("overlayChatInput");
  if (!input) return;

  // Reset to auto so the element can shrink if text was deleted
  input.style.height = "auto";

  // Calculate the new height, capped at 120px (max-height in CSS)
  const maxHeight = 120;
  const newHeight = Math.min(input.scrollHeight, maxHeight);
  input.style.height = newHeight + "px";
}


// ── Message management ──────────────────────────────────────

// Returns the total character count of the conversation history
function chatContextSize() {
  let total = 0;
  for (const m of chatMessages) total += m.content.length;
  return total;
}

// If the conversation is getting too long, start a new one
// This prevents the LLM from running out of context
function autoNewChat() {
  if (chatContextSize() > CHAT_CONTEXT_LIMIT) {
    chatMessages = [];
    appendSystemNote("Previous conversation archived — starting new chat.");
    return true;
  }
  return false;
}

// ── UI: Adding messages to the chat box ─────────────────────

// Adds a system message (grey, italic) to the chat box
function appendSystemNote(text) {
  const box = document.getElementById("overlayChatBox");
  const div = document.createElement("div");
  div.className = "chat-system";
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

// Adds a user or assistant message to the chat box
function appendMessage(role, text) {
  const box = document.getElementById("overlayChatBox");
  const div = document.createElement("div");
  div.className = role === "user" ? "chat-msg chat-user" : "chat-msg chat-ai";
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}

// Adds a "thinking..." placeholder while waiting for the LLM
function appendThinking() {
  const box = document.getElementById("overlayChatBox");
  const div = document.createElement("div");
  div.className = "chat-msg chat-ai chat-thinking";
  div.dataset.thinking = "true";
  div.textContent = "";
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}

// WHAT DOES THIS FUNCTION DO?
// Adds a tool execution result to the chat box.
// It shows up as a small system-style message with a badge
// indicating whether the tool succeeded or failed.
//
// Example: "✓ Created task: Study Python Basics" (green)
// Example: "✗ Failed to create task: title is required" (red)
function appendToolResult(toolCall) {
  const box = document.getElementById("overlayChatBox");
  const wrapper = document.createElement("div");
  wrapper.className = "chat-tool-result";

  // Status badge: checkmark for success, X for failure
  const badge = document.createElement("span");
  badge.className = "chat-tool-badge " + (toolCall.success ? "chat-tool-ok" : "chat-tool-fail");
  badge.textContent = toolCall.success ? "✓" : "✗";
  wrapper.appendChild(badge);

  // Tool name label (e.g., "create_task")
  const nameLabel = document.createElement("span");
  nameLabel.className = "chat-tool-name";
  nameLabel.textContent = toolCall.tool;
  wrapper.appendChild(nameLabel);

  // Result message (e.g., "Created task: Study Python Basics")
  const messageLabel = document.createElement("span");
  messageLabel.className = "chat-tool-message";
  messageLabel.textContent = toolCall.message || "";
  wrapper.appendChild(messageLabel);

  box.appendChild(wrapper);
  box.scrollTop = box.scrollHeight;
}

// ── Sending a message ───────────────────────────────────────

async function sendChat() {
  const input = document.getElementById("overlayChatInput");
  const btn = document.getElementById("overlaySendBtn");

  // Optionally include the currently selected task as context
  const includeTask = document.getElementById("includeTaskToggle")?.checked;
  const raw = input.value.trim();
  if (!raw) return;

  // Clear the input immediately so the user can type again
  input.value = "";
  autoResizeChatInput();

  // Show the user's message in the chat box
  appendMessage("user", raw);

  // If the user wants task context, prepend it to the message
  let content = raw;
  if (includeTask && activeId) {
    const task = notes.find(n => n.id === activeId);
    if (task) {
      content =
        `Task context — title: "${task.title}" | priority: ${task.priority} | template: ${task.template || "freeform"} | date: ${task.date}\n` +
        `Description: ${task.content || "(none)"}\n\n---\n\n` +
        raw;
    }
  }

  // Add to conversation history
  chatMessages.push({ role: "user", content });
  autoNewChat();

  // Disable the send button while waiting for a response
  btn.disabled = true;
  btn.textContent = "...";

  // Show a "thinking..." indicator
  const thinkingEl = appendThinking();

  try {
    // Get the current user's email for server-side task operations
    const currentUserEmail = localStorage.getItem("user") || "";

    // Send the conversation to the server
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-User-Email": currentUserEmail,  // Tell the server who is logged in
      },
      body: JSON.stringify({ messages: chatMessages, username: currentUserEmail }),
    });

    const data = await res.json();

    // Handle errors from the server
    if (!res.ok || !data.response) {
      thinkingEl.classList.remove("chat-thinking");
      thinkingEl.textContent = `Error: ${data.error || "Request failed"}`;
      return;
    }

    // Remove the "thinking" indicator
    thinkingEl.classList.remove("chat-thinking");

    // WHAT HAPPENS HERE?
    // The server might have executed tools before producing the final response.
    // We show each tool result as a small system-style message first.
    // Then we show the LLM's actual response text.
    if (data.tool_calls_made && data.tool_calls_made.length > 0) {
      // Show each tool result
      for (const toolCall of data.tool_calls_made) {
        appendToolResult(toolCall);
      }

      // Add a small note that the bot took action(s)
      const actionNote = document.createElement("div");
      actionNote.className = "chat-system chat-tool-summary";
      const count = data.tool_calls_made.length;
      actionNote.textContent = count === 1
        ? "I took 1 action:"
        : `I took ${count} actions:`;
      thinkingEl.parentNode.insertBefore(actionNote, thinkingEl);
    }

    // Show the LLM's final response text
    thinkingEl.textContent = data.response;

    // Add the response to conversation history
    chatMessages.push({ role: "assistant", content: data.response });

    // Show which AI provider/model was used
    if (data.provider && data.model) {
      const info = document.getElementById("chatProviderInfo");
      if (info) info.textContent = `${data.provider} / ${data.model}`;
    }

    // After tool calls, refresh the task list from the server
    // so the UI shows any newly created/modified tasks
    if (data.tool_calls_made && data.tool_calls_made.length > 0) {
      await syncTasksFromServer();
    }
  } catch (e) {
    thinkingEl.classList.remove("chat-thinking");
    thinkingEl.textContent = `Network error: ${e.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Send";
  }
}

// ── Clear chat ──────────────────────────────────────────────

function clearChat() {
  chatMessages = [];
  const box = document.getElementById("overlayChatBox");
  box.innerHTML = "";
  appendSystemNote("New conversation started.");
}

// ── Task sync ───────────────────────────────────────────────

// WHAT DOES THIS FUNCTION DO?
// After the chatbot creates or modifies tasks, we need to update
// the task list in the browser so the user sees the changes.
// This function fetches the latest tasks from the server and
// updates the local notes array and UI.
async function syncTasksFromServer() {
  try {
    const currentUserEmail = localStorage.getItem("user") || "";
    const res = await fetch("/api/tasks", {
      headers: { "X-User-Email": currentUserEmail },
    });
    if (!res.ok) return;

    const data = await res.json();
    const serverTasks = data.tasks || [];

    // Merge: server tasks become the source of truth
    // But we preserve the activeId if it still exists
    const currentActiveId = activeId;
    notes = serverTasks;

    // Also update localStorage so page reloads keep the data
    saveNotes();

    // Re-render the task list
    renderList();
    generateCalendar();
    scanTemplates();

    // If the previously active task still exists, keep it selected
    // Otherwise, select the first task (if any)
    if (currentActiveId && notes.find(n => n.id === currentActiveId)) {
      activeId = currentActiveId;
    } else if (notes.length > 0) {
      activeId = notes[0].id;
    } else {
      activeId = null;
    }
    renderList();
    loadTask();
    updateChatTaskLabel();
  } catch (e) {
    console.warn("Failed to sync tasks from server:", e);
  }
}
