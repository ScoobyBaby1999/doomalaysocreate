// ═══════════════════════════════════════════════════
// OUTPUTS — list and view generated task outputs
// ═══════════════════════════════════════════════════

async function loadOutputsList() {
  document.getElementById("outputsList").style.display = "";
  document.getElementById("outputViewer").style.display = "none";

  const listEl = document.getElementById("outputsListContent");
  const emptyEl = document.getElementById("outputsEmpty");

  listEl.innerHTML = '<div class="outputs-loading">Loading...</div>';
  emptyEl.style.display = "none";

  try {
    const res = await fetch("/api/outputs");
    if (!res.ok) throw new Error("Failed to load outputs");
    const data = await res.json();

    if (!data.outputs || data.outputs.length === 0) {
      listEl.innerHTML = "";
      emptyEl.style.display = "";
      return;
    }

    listEl.innerHTML = data.outputs
      .map(
        (o) =>
          '<div class="output-item" onclick="viewOutput(\'' +
          escapeAttr(o.file) +
          "')\">" +
          '<div class="output-item-name">' +
          escapeHtml(o.name) +
          "</div>" +
          '<div class="output-item-meta">' +
          o.modified +
          " - " +
          o.word_count +
          " words - " +
          o.size_kb +
          " KB" +
          "</div>" +
          "</div>"
      )
      .join("");
  } catch (e) {
    listEl.innerHTML =
      '<div class="outputs-error">Failed to load outputs: ' +
      escapeHtml(e.message) +
      "</div>";
  }
}

async function viewOutput(file) {
  const name = file.replace(".md", "");

  document.getElementById("outputsList").style.display = "none";
  document.getElementById("outputViewer").style.display = "";

  const metaEl = document.getElementById("outputMeta");
  const bodyEl = document.getElementById("outputBody");

  metaEl.innerHTML = '<div class="outputs-loading">Loading...</div>';
  bodyEl.innerHTML = "";

  try {
    const res = await fetch("/api/outputs/" + encodeURIComponent(name));
    if (!res.ok) throw new Error("Output not found");
    const data = await res.json();

    const fm = data.frontmatter || {};
    const metaParts = [];
    if (fm.task_type) metaParts.push("Type: " + fm.task_type);
    if (fm.credits) metaParts.push("Provider: " + fm.credits.replace(/generator via /, ""));
    if (fm.created) metaParts.push("Created: " + fm.created);
    metaParts.push(data.word_count + " words");

    metaEl.innerHTML =
      '<h2 class="output-title">' + escapeHtml(data.name) + "</h2>" +
      '<div class="output-meta-tags">' +
      metaParts.map((p) => '<span class="output-tag">' + escapeHtml(p) + "</span>").join("") +
      "</div>";

    bodyEl.innerHTML = renderMarkdown(data.body);
  } catch (e) {
    bodyEl.innerHTML =
      '<div class="outputs-error">Failed to load: ' +
      escapeHtml(e.message) +
      "</div>";
  }
}

function backToOutputsList() {
  loadOutputsList();
}

// ── Simple markdown renderer (no CDN) ──────────────

function renderMarkdown(text) {
  if (!text) return "";

  const lines = text.split("\n");
  let html = "";
  let inList = false;
  let inCodeBlock = false;
  let codeContent = "";

  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];

    if (line.startsWith("```")) {
      if (inCodeBlock) {
        html += "<pre class=\"output-code\">" + escapeHtml(codeContent) + "</pre>";
        codeContent = "";
        inCodeBlock = false;
      } else {
        if (inList) {
          html += "</ul>";
          inList = false;
        }
        inCodeBlock = true;
      }
      continue;
    }

    if (inCodeBlock) {
      codeContent += line + "\n";
      continue;
    }

    if (line === "") {
      if (inList) {
        html += "</ul>";
        inList = false;
      }
      continue;
    }

    const headingMatch = line.match(/^(#{1,6})\s+(.*)/);
    if (headingMatch) {
      if (inList) {
        html += "</ul>";
        inList = false;
      }
      const level = headingMatch[1].length;
      html += "<h" + level + ">" + inlineFormat(headingMatch[2]) + "</h" + level + ">";
      continue;
    }

    if (line.match(/^[-*]\s+/)) {
      if (!inList) {
        html += "<ul>";
        inList = true;
      }
      html += "<li>" + inlineFormat(line.replace(/^[-*]\s+/, "")) + "</li>";
      continue;
    }

    const orderedMatch = line.match(/^\d+\.\s+(.*)/);
    if (orderedMatch) {
      if (inList) {
        html += "</ul>";
        inList = false;
      }
      html += "<p>" + inlineFormat(orderedMatch[1]) + "</p>";
      continue;
    }

    if (inList) {
      html += "</ul>";
      inList = false;
    }
    html += "<p>" + inlineFormat(line) + "</p>";
  }

  if (inList) html += "</ul>";
  if (inCodeBlock) html += "<pre class=\"output-code\">" + escapeHtml(codeContent) + "</pre>";

  return html;
}

function inlineFormat(text) {
  text = escapeHtml(text);
  text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/\*(.+?)\*/g, "<em>$1</em>");
  text = text.replace(/`(.+?)`/g, "<code>$1</code>");
  return text;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.appendChild(document.createTextNode(str));
  return div.innerHTML;
}

function escapeAttr(str) {
  return str.replace(/'/g, "\\'").replace(/"/g, "&quot;");
}
