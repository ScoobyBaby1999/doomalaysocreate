"""
server.py — lightweight HTTP server for timemanager.

WHAT DOES THIS FILE DO?
It is the backend that handles all API requests from the browser:
- Serves static HTML/CSS/JS files
- Manages tasks (create, read, update, delete)
- Manages templates (list, create, delete)
- Runs the AI orchestrator on tasks
- Powers the chatbot with tool-use (create tasks, run tasks, etc.)
- Saves and loads user data to disk

HOW DOES THE CHATBOT TOOL-USE WORK?
1. User sends a message like "Create a task called Study Python"
2. The LLM receives the message plus a list of available tools
3. If the LLM wants to do something, it writes: [TOOL_CALL: {"tool": "create_task", "args": {...}}]
4. The server detects that pattern, extracts the JSON, and runs the actual function
5. The result is sent back to the LLM, which then writes a friendly reply to the user
"""
from __future__ import annotations
import asyncio
import io
import json
import mimetypes
import random
import re
import sys
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

# ─────────────────────────────────────────────────────────────────
# DIRECTORY PATHS
# ─────────────────────────────────────────────────────────────────

# The root folder of this project (where server.py lives)
ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
TEMPLATES_DIR = BACKEND / "templates"
PROMPTS_DIR = BACKEND / "prompts"

# Where we store user data on disk (tasks, settings, etc.)
# Each user gets their own JSON file: data/<username>.json
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Insert the backend folder into Python's import path
# This lets us do "from runner import process_one" etc.
sys.path.insert(0, str(BACKEND))

# MIME types for common static assets
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/html", ".html")


# ═══════════════════════════════════════════════════════════════
# SECTION 1: USER DATA STORAGE
# ═══════════════════════════════════════════════════════════════
#
# WHAT DOES THIS SECTION DO?
# Tasks used to live only in the browser's localStorage. That meant
# the server (and the chatbot) couldn't see or modify them. Now we
# save a copy to disk so every part of the system can read/write tasks.
#
# Each user's data is stored in: data/<username>.json
# The username comes from the login system (the user's email address).

def get_user_data_path(username: str) -> Path:
    """
    WHAT DOES THIS FUNCTION DO?
    Given a username (like "alice@email.com"), return the path to
    that user's data file. We replace @ with _ to keep filenames safe.

    Example: "alice@email.com" → data/alice_email_com.json
    """
    # Replace characters that are not allowed in filenames
    safe_name = username.replace("@", "_at_").replace(".", "_")
    return DATA_DIR / f"{safe_name}.json"


def load_user_data(username: str) -> dict:
    """
    WHAT DOES THIS FUNCTION DO?
    Load a user's data from disk. Returns a dictionary with two keys:
    - "tasks": list of task objects (same format as localStorage)
    - "settings": optional user preferences

    If the file doesn't exist yet (new user), returns empty defaults.
    """
    filepath = get_user_data_path(username)
    if not filepath.exists():
        # New user — return empty defaults
        return {"tasks": [], "settings": {}}
    try:
        return json.loads(filepath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # File is corrupted or unreadable — return empty defaults
        return {"tasks": [], "settings": {}}


def save_user_data(username: str, data: dict) -> None:
    """
    WHAT DOES THIS FUNCTION DO?
    Save a user's data to disk. Uses atomic write (write to temp file,
    then rename) so the file is never half-written if the server crashes.
    """
    filepath = get_user_data_path(username)
    temp_file = filepath.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp_file.replace(filepath)


# ═══════════════════════════════════════════════════════════════
# SECTION 2: API ENDPOINTS — TEMPLATES (list, create, delete)
# ═══════════════════════════════════════════════════════════════
#
# WHAT DOES THIS SECTION DO?
# These functions handle HTTP requests related to templates:
# - GET  /api/templates  → list all available template files
# - POST /api/templates  → create a new template file
# - DELETE /api/templates/<name> → delete a template file


def api_list_templates() -> tuple[int, dict]:
    """
    WHAT DOES THIS FUNCTION DO?
    Lists all .json template files in the templates directory.
    Returns the template name, filename, and relative path.
    """
    if not TEMPLATES_DIR.exists():
        return 500, {"error": f"templates dir not found: {TEMPLATES_DIR}"}
    files = sorted(TEMPLATES_DIR.glob("*.json"))
    return 200, {
        "templates": [
            {
                "name": f.stem,
                "file": f.name,
                "path": str(f.relative_to(ROOT)),
            }
            for f in files
        ]
    }


def api_create_template(payload: dict) -> tuple[int, dict]:
    """
    WHAT DOES THIS FUNCTION DO?
    Creates a new template file on disk from the JSON data sent by the client.

    Expected payload:
    {
        "name": "my_custom_template",       (required — lowercase with underscores)
        "content": { ... template JSON ... }  (required — the actual template data)
    }

    The function validates:
    1. Name is provided and is a valid filename (alphanumeric + underscores)
    2. Content is a valid dictionary
    3. Content has required fields: task_type, stages
    4. Each stage has required fields: name, role, instructions

    Then writes the template as a .json file to backend/templates/
    """
    # Step 1: Get and validate the template name
    template_name = (payload.get("name") or "").strip()
    if not template_name:
        return 400, {"error": "template name is required"}

    # Only allow letters, numbers, and underscores (safe filename)
    if not re.match(r"^[a-zA-Z0-9_]+$", template_name):
        return 400, {"error": "name must only contain letters, numbers, and underscores"}

    # Step 2: Get and validate the template content
    template_content = payload.get("content")
    if not template_content or not isinstance(template_content, dict):
        return 400, {"error": "content must be a valid JSON object"}

    # Step 3: Check that required fields exist
    if "task_type" not in template_content:
        return 400, {"error": "template must have a 'task_type' field"}
    if "stages" not in template_content:
        return 400, {"error": "template must have a 'stages' field"}
    if not isinstance(template_content["stages"], list):
        return 400, {"error": "'stages' must be a list"}
    if len(template_content["stages"]) == 0:
        return 400, {"error": "template must have at least one stage"}

    # Step 4: Validate each stage has the minimum required fields
    for i, stage in enumerate(template_content["stages"]):
        if not isinstance(stage, dict):
            return 400, {"error": f"stage {i} must be a JSON object"}
        if "name" not in stage:
            return 400, {"error": f"stage {i} must have a 'name' field"}
        if "role" not in stage:
            return 400, {"error": f"stage {i} must have a 'role' field"}
        if "instructions" not in stage:
            return 400, {"error": f"stage {i} must have an 'instructions' field"}

    # Step 5: Write the template file to disk
    template_path = TEMPLATES_DIR / f"{template_name}.json"
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    # Write with nice formatting (2-space indent)
    template_path.write_text(
        json.dumps(template_content, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return 200, {"ok": True, "name": template_name, "path": str(template_path)}


def api_delete_template(template_name: str) -> tuple[int, dict]:
    """
    WHAT DOES THIS FUNCTION DO?
    Deletes a template file from disk by name.

    Safety: We check that the file exists first, and we sanitize the
    name so someone can't delete files outside the templates directory.
    """
    # Only allow safe filenames (no directory traversal like ../../etc/passwd)
    safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in template_name)
    template_path = TEMPLATES_DIR / f"{safe_name}.json"

    if not template_path.exists():
        return 404, {"error": f"template not found: {safe_name}"}

    template_path.unlink()
    return 200, {"ok": True, "name": safe_name}


# ═══════════════════════════════════════════════════════════════
# SECTION 3: API ENDPOINTS — TASKS (list, create, update, delete)
# ═══════════════════════════════════════════════════════════════
#
# WHAT DOES THIS SECTION DO?
# These functions handle HTTP requests related to tasks.
# They read/write the user's data file on disk.
#
# - GET    /api/tasks           → list all tasks for the logged-in user
# - POST   /api/tasks           → create a new task
# - PUT    /api/tasks/<id>      → update an existing task
# - DELETE /api/tasks/<id>      → delete a task


def _get_current_username_from_request(handler) -> str:
    """
    WHAT DOES THIS FUNCTION DO?
    Extracts the username from the incoming HTTP request.

    HOW DOES IT WORK?
    The browser sends the username in a header called "X-User-Email".
    This is set by the JavaScript client on page load from localStorage.

    If the header is missing (shouldn't happen for logged-in users),
    we fall back to a default username so the server doesn't crash.
    """
    return handler.headers.get("X-User-Email", "default_user")


def api_list_tasks(username: str) -> tuple[int, dict]:
    """
    WHAT DOES THIS FUNCTION DO?
    Returns all tasks for the given user. The tasks come from the
    user's data file on disk (data/<username>.json).
    """
    data = load_user_data(username)
    return 200, {"tasks": data.get("tasks", [])}


def api_create_task(username: str, payload: dict) -> tuple[int, dict]:
    """
    WHAT DOES THIS FUNCTION DO?
    Creates a new task and saves it to the user's data file.

    Expected payload (only "title" is required):
    {
        "title": "Study Python Basics",     (required)
        "content": "Learn about functions",  (optional — task description)
        "priority": "medium",                (optional — high/medium/low)
        "template": "freeform",              (optional — template name)
        "date": "2026-05-05"                 (optional — due date)
    }

    Returns the complete task object including the auto-generated ID.
    """
    # Step 1: Validate required fields
    title = (payload.get("title") or "").strip()
    if not title:
        return 400, {"error": "title is required"}

    # Step 2: Build the task object
    # We generate a unique ID using the current time + random number
    # This avoids conflicts even if two tasks are created at the same moment
    task_id = int(time.time() * 1000) + random.randint(0, 999)

    # Get today's date as the default
    today = time.strftime("%Y-%m-%d")

    task = {
        "id": task_id,
        "title": title,
        "content": (payload.get("content") or payload.get("description") or "").strip(),
        "priority": payload.get("priority") or "medium",
        "template": payload.get("template") or "freeform",
        "date": payload.get("date") or today,
    }

    # Step 3: Load existing data, append the new task, save back
    data = load_user_data(username)
    data["tasks"].append(task)
    save_user_data(username, data)

    return 201, {"ok": True, "task": task}


def api_update_task(username: str, task_id: int, payload: dict) -> tuple[int, dict]:
    """
    WHAT DOES THIS FUNCTION DO?
    Updates specific fields of an existing task. Only the fields
    included in the payload are changed — everything else stays the same.

    Example payload to update just the priority:
    { "priority": "high" }

    Example payload to update title and description:
    { "title": "New Title", "content": "New description" }
    """
    # Step 1: Load the user's data
    data = load_user_data(username)
    tasks = data.get("tasks", [])

    # Step 2: Find the task by ID
    matching_tasks = [t for t in tasks if t.get("id") == task_id]
    if not matching_tasks:
        return 404, {"error": f"task {task_id} not found"}

    task = matching_tasks[0]

    # Step 3: Update only the fields that were provided in the payload
    # We check each possible field individually
    if "title" in payload and payload["title"]:
        task["title"] = payload["title"].strip()
    if "content" in payload:
        task["content"] = payload["content"].strip() if payload["content"] else ""
    elif "description" in payload:
        task["content"] = payload["description"].strip() if payload["description"] else ""
    if "priority" in payload and payload["priority"]:
        task["priority"] = payload["priority"].strip()
    if "template" in payload and payload["template"]:
        task["template"] = payload["template"].strip()
    if "date" in payload and payload["date"]:
        task["date"] = payload["date"].strip()

    # Step 4: Save the updated data back to disk
    save_user_data(username, data)

    return 200, {"ok": True, "task": task}


def api_delete_task(username: str, task_id: int) -> tuple[int, dict]:
    """
    WHAT DOES THIS FUNCTION DO?
    Removes a task from the user's data file by ID.
    """
    data = load_user_data(username)
    tasks = data.get("tasks", [])

    # Count how many tasks match (should be 0 or 1)
    before_count = len(tasks)
    tasks = [t for t in tasks if t.get("id") != task_id]
    after_count = len(tasks)

    if before_count == after_count:
        return 404, {"error": f"task {task_id} not found"}

    data["tasks"] = tasks
    save_user_data(username, data)

    return 200, {"ok": True}


# ═══════════════════════════════════════════════════════════════
# SECTION 4: API ENDPOINTS — OUTPUTS (list, read)
# ═══════════════════════════════════════════════════════════════
#
# WHAT DOES THIS SECTION DO?
# Lists and reads the markdown files produced by the AI orchestrator.
# These are the actual results/outputs of processed tasks.

OUTPUTS_DIR = BACKEND / "outputs"


def api_list_outputs() -> tuple[int, dict]:
    """Lists all .md output files in backend/outputs/"""
    if not OUTPUTS_DIR.exists():
        return 500, {"error": f"outputs dir not found: {OUTPUTS_DIR}"}
    files = []
    for f in sorted(OUTPUTS_DIR.glob("*.md")):
        if f.name.startswith("_"):
            continue  # Skip internal files (like _orchestrator.jsonl)
        stat = f.stat()
        raw = f.read_text(encoding="utf-8")
        word_count = len(raw.split())
        files.append({
            "name": f.stem,
            "file": f.name,
            "modified": time.strftime("%Y-%m-%d", time.localtime(stat.st_mtime)),
            "size_kb": round(stat.st_size / 1024, 1),
            "word_count": word_count,
        })
    return 200, {"outputs": files}


def api_get_output(name: str) -> tuple[int, dict]:
    """
    Reads a single output markdown file and parses its frontmatter.
    Frontmatter is the metadata at the top between --- markers, like:
    ---
    title: My Task
    priority: high
    ---
    """
    safe_name = "".join(c if c.isalnum() or c in " _-." else "_" for c in name)
    filepath = OUTPUTS_DIR / f"{safe_name}.md"
    if not filepath.exists():
        return 404, {"error": f"output not found: {safe_name}.md"}

    raw = filepath.read_text(encoding="utf-8")

    frontmatter = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            fm_text = parts[1].strip()
            body = parts[2].strip()
            for line in fm_text.splitlines():
                if ":" in line and not line.startswith("-"):
                    k, v = line.split(":", 1)
                    frontmatter[k.strip()] = v.strip().strip("'\"")

    return 200, {
        "name": safe_name,
        "frontmatter": frontmatter,
        "body": body,
        "word_count": len(body.split()),
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 5: API ENDPOINTS — LOG (read, clear)
# ═══════════════════════════════════════════════════════════════
#
# WHAT DOES THIS SECTION DO?
# The orchestrator writes events to a log file (_orchestrator.jsonl)
# as it processes tasks. These endpoints let the UI read and clear
# that log file in real time.

ORCHESTRATOR_LOG = OUTPUTS_DIR / "_orchestrator.jsonl"


def api_get_log() -> tuple[int, dict]:
    """
    Reads the orchestrator log file and returns all entries as JSON.
    Entries are returned newest-first so the UI shows the latest events at top.
    """
    if not ORCHESTRATOR_LOG.exists():
        return 200, {"entries": []}
    entries = []
    for line in ORCHESTRATOR_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    entries.reverse()  # Newest first
    return 200, {"entries": entries}


def api_clear_log() -> tuple[int, dict]:
    """Deletes all content from the orchestrator log file."""
    if ORCHESTRATOR_LOG.exists():
        try:
            ORCHESTRATOR_LOG.write_text("", encoding="utf-8")
        except OSError as e:
            return 500, {"error": f"failed to clear log: {str(e)}"}
    return 200, {"ok": True}


# ═══════════════════════════════════════════════════════════════
# SECTION 6: API ENDPOINT — PROCESS (run the orchestrator)
# ═══════════════════════════════════════════════════════════════
#
# WHAT DOES THIS SECTION DO?
# When a user clicks "Process (AI)" on a task, this endpoint:
# 1. Creates a prompt file from the task data
# 2. Loads the AI template (schematic)
# 3. Runs the orchestrator pipeline (multi-stage LLM execution)
# 4. Saves the output to backend/outputs/


async def api_process(payload: dict) -> tuple[int, dict]:
    """
    WHAT DOES THIS FUNCTION DO?
    Takes a task's data, builds a prompt file, and runs the AI orchestrator.

    Expected payload:
    {
        "title": "Study Python",
        "body": "Write a lesson about Python functions...",
        "priority": "medium",
        "template": "lesson_plan",
        "date": "2026-05-05"
    }
    """
    title = (payload.get("title") or "").strip()
    body = (payload.get("body") or "").strip()
    priority = (payload.get("priority") or "medium").strip()
    template = (payload.get("template") or "").strip()
    date = (payload.get("date") or "").strip()

    if not title:
        return 400, {"error": "title is required"}
    if not body:
        return 400, {"error": "body is required"}

    # Build a prompt file from the task
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitize the title into a safe filename
    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in title).strip()
    prompt_path = PROMPTS_DIR / f"{safe_name}.md"

    # Build the file content: frontmatter (metadata) + the actual task body
    frontmatter = f"---\ntitle: {title}\npriority: {priority}\n"
    if date:
        frontmatter += f"date: '{date}'\n"
    if template:
        frontmatter += f"template: {template}\n"
    frontmatter += "---\n\n"

    prompt_path.write_text(frontmatter + body, encoding="utf-8")

    # Delete any cached schematic so the current template selection is respected
    cache_path = prompt_path.with_suffix(prompt_path.suffix + ".schematic.json")
    if cache_path.exists():
        cache_path.unlink()

    # Run the orchestrator for this single prompt
    try:
        from runner import process_one, Entry, load_state, save_state
        from scheduler import SlotScheduler
        import providers

        state = load_state()
        stem = prompt_path.stem
        entry = state.setdefault(stem, Entry(stem=stem))

        provs = providers.make_provider_registry()
        slots = providers.make_slot_registry(provs)
        if not slots:
            return 500, {"error": "no LLM slots configured — check .env for API keys"}

        scheduler = SlotScheduler(slots)

        import httpx
        async with httpx.AsyncClient(timeout=600.0) as client:
            await process_one(client, scheduler, prompt_path, state, dry=False)

        updated = state.get(stem)
        return 200, {
            "status": updated.status if updated else "unknown",
            "word_count": updated.word_count if updated else 0,
            "error": updated.last_error if updated else None,
            "output_file": f"backend/outputs/{stem}.md" if updated and updated.status == "done" else None,
        }
    except Exception as e:
        return 500, {"error": f"orchestrator failed: {str(e)}"}


# ═══════════════════════════════════════════════════════════════
# SECTION 7: CHATBOT TOOL-USE SYSTEM
# ═══════════════════════════════════════════════════════════════
#
# WHAT DOES THIS SECTION DO?
# This is the core of the chatbot's ability to actually DO things.
# Previously, the chatbot could only talk. Now it can:
#   - Create, update, delete, and list tasks
#   - Create and delete templates
#   - Run tasks through the orchestrator
#
# HOW DOES IT WORK?
# 1. We send the LLM a system prompt that lists all available "tools"
#    (like create_task, update_task, etc.) and explains how to use them
# 2. When the LLM wants to use a tool, it writes a special marker:
#    [TOOL_CALL: {"tool": "create_task", "args": {"title": "Study Python"}}]
# 3. Our server detects that marker with a regex pattern, extracts the JSON,
#    and calls the corresponding Python function
# 4. The result is sent back to the LLM in the conversation
# 5. The LLM then writes a friendly reply to the user
#
# This loops up to 5 times so the LLM can chain multiple tool calls
# (e.g., create_task → run_task) before producing a final answer.


# ── TOOL DEFINITIONS ───────────────────────────────────────────
#
# WHAT IS THIS?
# This is the text sent to the LLM explaining what tools it can use.
# The LLM reads this to understand:
#   - What actions it can take (create tasks, run them, etc.)
#   - What information each tool needs (arguments)
#   - How to format a tool call (the [TOOL_CALL: ...] syntax)
#
# IMPORTANT: This must be very clear so the LLM doesn't invent
# tool names or arguments that don't exist.

TOOL_DEFINITIONS_TEXT = """
You have access to the following TOOLS. Use them when the user wants you to perform an action.

HOW TO USE A TOOL:
Write this exact format on its own line:
[TOOL_CALL: {"tool": "tool_name", "args": {"argument_name": "value"}}]

EXAMPLES:
[TOOL_CALL: {"tool": "create_task", "args": {"title": "Study Python Basics", "description": "Learn about variables, loops, and functions. Write a beginner-friendly lesson covering syntax, data types, control flow, and practical exercises.", "priority": "medium", "template": "lesson_plan"}}]
[TOOL_CALL: {"tool": "list_tasks", "args": {}}]
[TOOL_CALL: {"tool": "update_task", "args": {"task_id": 1234567890, "priority": "high"}}]
[TOOL_CALL: {"tool": "run_task", "args": {"task_id": 1234567890}}]
[TOOL_CALL: {"tool": "list_templates", "args": {}}]
[TOOL_CALL: {"tool": "get_template", "args": {"name": "research_paper"}}]

AVAILABLE TOOLS:

1. create_task — Create a new task in the Tasks list
   Required args: title (string)
   Optional args: description (string — the task body/prompt that the AI orchestrator will process; be detailed and specific about what you want generated), priority ("high", "medium", or "low"), template (template name — use list_templates to see available ones), date (YYYY-MM-DD)
   IMPORTANT: The "description" field is what the AI uses as its prompt when running the task. Include all the details, instructions, and context here. Do NOT leave it empty.

2. update_task — Modify an existing task
   Required args: task_id (integer — the ID of the task to update)
   Optional args: title (string), description (string), priority ("high"/"medium"/"low"), template (string), date (YYYY-MM-DD)
   Note: Only include the fields you want to change. Omitted fields stay the same.

3. delete_task — Remove a task
   Required args: task_id (integer)

4. list_tasks — Show all current tasks
   No arguments needed: {}

5. create_template — Create a new AI pipeline template
   Required args: name (string, lowercase with underscores), stages (list of stage objects)
   Optional args: output_rules (object)
   Each stage object MUST have: name (string, unique within template), role (MUST be one of: planner, parser, critiquer, verifier, generator, transformer, assembler), instructions (string — the prompt/directive for this stage)
   Each stage can optionally have: inputs (list of context path strings like "previous_stage" or "prompt"), max_tokens (integer), fanout ({"over": "context_path", "max_parallel": int})

6. delete_template — Remove a template
   Required args: template_name (string)

7. run_task — Execute a task through the AI orchestrator pipeline
   Required args: task_id (integer)
   Optional args: template (string — overrides the task's default template)

8. list_templates — List all available templates with their names, descriptions, and stage info
   No arguments needed: {}
   Returns: each template's name, task_type, description (what it does), stage_count, stage_names

9. get_template — Get the full details of a specific template (stages, roles, inputs, output_rules)
   Required args: name (string — the template name like "research_paper" or "freeform")

IMPORTANT RULES:
- You can use multiple tools in one response (one per line)
- After using a tool, you will see the result and can continue the conversation
- If you don't know a task_id, use list_tasks first to find it
- Before recommending a template, ALWAYS use list_templates to see what exists
- Before creating a template, check if a similar one already exists with list_templates
- Only use tools when the user actually wants you to do something
- For general questions, just answer normally without tools
- When the user asks you to modify, update, or change a task, ALWAYS chain tools:
  1. Call list_tasks to find the task_id
  2. Call update_task (or delete_task/run_task) with that task_id — in the SAME response
  Never just list a task and claim you already changed it. You must actually call the tool.
- If the user's request is ambiguous, call list_tasks first and wait for confirmation
"""


# ── TOOL-CALL PARSER ───────────────────────────────────────────
#
# WHAT DOES THIS FUNCTION DO?
# Scans the LLM's response text looking for [TOOL_CALL: ...] patterns.
# When it finds one, it extracts the JSON inside and returns it
# as a Python dictionary.
#
# HOW DOES IT WORK?
# It uses a regex (regular expression) pattern to find text that looks like:
#   [TOOL_CALL: {"tool": "create_task", "args": {...}}]
# Then it extracts the JSON part and parses it with json.loads().
#
# If the text has no tool calls, it returns an empty list.
# If the JSON is malformed, that specific tool call is skipped.

def parse_tool_calls(llm_response_text: str) -> list[dict]:
    """
    WHAT DOES THIS FUNCTION DO?
    Extracts all [TOOL_CALL: {...}] blocks from the LLM's response.

    Uses brace-counting instead of regex so that ] characters inside
    JSON arrays or strings don't break the parser.

    Returns a list of dictionaries, each with:
    - "tool": the tool name (e.g., "create_task")
    - "args": the arguments dictionary (e.g., {"title": "Study Python"})

    If no tool calls are found, returns an empty list.
    """
    tool_calls = []
    start_marker = "[TOOL_CALL:"
    idx = 0

    while True:
        pos = llm_response_text.find(start_marker, idx)
        if pos == -1:
            break

        # Find the opening brace of the JSON object
        brace_start = llm_response_text.find("{", pos)
        if brace_start == -1:
            idx = pos + len(start_marker)
            continue

        # Brace-counting to find the matching }, respecting strings
        depth = 0
        i = brace_start
        in_string = False
        escape_next = False
        end_pos = -1

        while i < len(llm_response_text):
            ch = llm_response_text[i]
            if escape_next:
                escape_next = False
                i += 1
                continue
            if ch == "\\":
                if in_string:
                    escape_next = True
                i += 1
                continue
            if ch == '"':
                in_string = not in_string
            elif not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end_pos = i
                        break
            i += 1

        if end_pos == -1:
            idx = pos + len(start_marker)
            continue

        json_text = llm_response_text[brace_start:end_pos + 1]
        try:
            parsed = json.loads(json_text)
            if isinstance(parsed, dict) and "tool" in parsed:
                tool_calls.append({
                    "tool": parsed["tool"],
                    "args": parsed.get("args", {}),
                })
        except json.JSONDecodeError:
            pass

        idx = end_pos + 1

    return tool_calls


# ── TOOL EXECUTION DISPATCHER ──────────────────────────────────
#
# WHAT DOES THIS FUNCTION DO?
# Takes a tool call (name + arguments) and runs the actual Python function.
# It's like a switchboard operator: "Oh, you want create_task? I'll connect
# you to the create_task function."
#
# Each tool returns a result dictionary with:
# - "success": True/False (did it work?)
# - "message": A human-readable summary of what happened
# - "data": The actual data (task object, list of tasks, etc.) on success
# - "error": Error message on failure


def execute_tool(tool_name: str, tool_args: dict, username: str) -> dict:
    """
    WHAT DOES THIS FUNCTION DO?
    Routes a tool call to the correct function based on the tool name.

    Arguments:
    - tool_name: The name of the tool (e.g., "create_task")
    - tool_args: The arguments the LLM provided (e.g., {"title": "Study Python"})
    - username: The logged-in user's email (so we know whose data to modify)

    Returns a dictionary with the result of the tool execution.
    """
    # Route to the correct function based on tool name
    if tool_name == "create_task":
        return _tool_create_task(tool_args, username)
    elif tool_name == "update_task":
        return _tool_update_task(tool_args, username)
    elif tool_name == "delete_task":
        return _tool_delete_task(tool_args, username)
    elif tool_name == "list_tasks":
        return _tool_list_tasks(username)
    elif tool_name == "create_template":
        return _tool_create_template(tool_args)
    elif tool_name == "delete_template":
        return _tool_delete_template(tool_args)
    elif tool_name == "run_task":
        return _tool_run_task(tool_args, username)
    elif tool_name == "list_templates":
        return _tool_list_templates()
    elif tool_name == "get_template":
        return _tool_get_template(tool_args)
    else:
        # The LLM tried to use a tool that doesn't exist
        return {
            "success": False,
            "message": f"Unknown tool: {tool_name}. Available tools: create_task, update_task, delete_task, list_tasks, create_template, delete_template, run_task.",
            "error": f"Tool '{tool_name}' does not exist",
        }


def _tool_create_task(args: dict, username: str) -> dict:
    """
    WHAT DOES THIS FUNCTION DO?
    Creates a new task by calling the api_create_task function.
    This is the "worker" function that execute_tool() calls when the tool is "create_task".
    """
    status_code, result = api_create_task(username, args)
    if status_code == 201:
        return {
            "success": True,
            "message": f"Created task: '{args.get('title', 'Untitled')}' (ID: {result['task']['id']})",
            "data": result["task"],
        }
    return {
        "success": False,
        "message": f"Failed to create task: {result.get('error', 'Unknown error')}",
        "error": result.get("error"),
    }


def _tool_update_task(args: dict, username: str) -> dict:
    """
    WHAT DOES THIS FUNCTION DO?
    Updates an existing task. The LLM must provide task_id and the fields to change.
    """
    task_id = args.get("task_id")
    if task_id is None:
        return {
            "success": False,
            "message": "Cannot update task: missing 'task_id' argument",
            "error": "task_id is required",
        }

    # Remove task_id from args so only the update fields remain
    update_fields = {k: v for k, v in args.items() if k != "task_id"}
    if not update_fields:
        return {
            "success": False,
            "message": "Cannot update task: no fields to update. Include at least one of: title, description, priority, template, date.",
            "error": "no fields to update",
        }

    status_code, result = api_update_task(username, int(task_id), update_fields)
    if status_code == 200:
        return {
            "success": True,
            "message": f"Updated task '{result['task'].get('title', 'Unknown')}' — changed: {', '.join(update_fields.keys())}",
            "data": result["task"],
        }
    return {
        "success": False,
        "message": f"Failed to update task: {result.get('error', 'Unknown error')}",
        "error": result.get("error"),
    }


def _tool_delete_task(args: dict, username: str) -> dict:
    """
    WHAT DOES THIS FUNCTION DO?
    Deletes a task by ID.
    """
    task_id = args.get("task_id")
    if task_id is None:
        return {
            "success": False,
            "message": "Cannot delete task: missing 'task_id' argument",
            "error": "task_id is required",
        }

    status_code, result = api_delete_task(username, int(task_id))
    if status_code == 200:
        return {
            "success": True,
            "message": f"Deleted task (ID: {task_id})",
        }
    return {
        "success": False,
        "message": f"Failed to delete task: {result.get('error', 'Unknown error')}",
        "error": result.get("error"),
    }


def _tool_list_tasks(username: str) -> dict:
    """
    WHAT DOES THIS FUNCTION DO?
    Returns a list of all the user's tasks so the LLM can see what exists.
    This is useful when the user says "show my tasks" or when the LLM
    needs to find a task_id before updating or running a task.
    """
    status_code, result = api_list_tasks(username)
    tasks = result.get("tasks", [])

    if not tasks:
        return {
            "success": True,
            "message": "No tasks found. The task list is empty.",
            "data": [],
        }

    # Build a compact summary for the LLM (don't send too much text)
    task_summary = []
    for t in tasks:
        task_summary.append({
            "id": t["id"],
            "title": t.get("title", "Untitled"),
            "priority": t.get("priority", "medium"),
            "template": t.get("template", "freeform"),
            "date": t.get("date", ""),
        })

    return {
        "success": True,
        "message": f"Found {len(tasks)} task(s)",
        "data": task_summary,
    }


def _tool_list_templates() -> dict:
    """
    WHAT DOES THIS FUNCTION DO?
    Returns a list of all available templates with their key details.
    This lets the LLM see what templates actually exist before recommending them.
    """
    if not TEMPLATES_DIR.exists():
        return {
            "success": False,
            "message": "Templates directory not found",
            "data": [],
        }

    files = sorted(TEMPLATES_DIR.glob("*.json"))
    templates = []
    for f in files:
        try:
            content = json.loads(f.read_text(encoding="utf-8"))
            templates.append({
                "name": f.stem,
                "task_type": content.get("task_type", "unknown"),
                "task": content.get("task", ""),
                "description": content.get("description", ""),
                "stage_count": len(content.get("stages", [])),
                "stage_names": [s.get("name", "?") for s in content.get("stages", [])],
                "has_output_rules": "output_rules" in content,
            })
        except (json.JSONDecodeError, OSError):
            templates.append({
                "name": f.stem,
                "task_type": "error",
                "task": "(could not read template)",
                "description": "",
                "stage_count": 0,
                "stage_names": [],
                "has_output_rules": False,
            })

    return {
        "success": True,
        "message": f"Found {len(templates)} template(s): {', '.join(t['name'] for t in templates)}",
        "data": templates,
    }


def _tool_get_template(args: dict) -> dict:
    """
    WHAT DOES THIS FUNCTION DO?
    Returns the full JSON content of a specific template.
    The LLM uses this to see exact stage definitions before recommending or modifying templates.
    """
    name = (args.get("name") or "").strip()
    if not name:
        return {
            "success": False,
            "message": "Cannot get template: missing 'name' argument",
            "error": "name is required",
        }

    # Sanitize the name to prevent directory traversal
    safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
    template_path = TEMPLATES_DIR / f"{safe_name}.json"

    if not template_path.exists():
        return {
            "success": False,
            "message": f"Template '{safe_name}' not found. Use list_templates to see available templates.",
            "error": f"template not found: {safe_name}",
        }

    try:
        content = json.loads(template_path.read_text(encoding="utf-8"))
        return {
            "success": True,
            "message": f"Loaded template: {safe_name} ({len(content.get('stages', []))} stages)",
            "data": content,
        }
    except (json.JSONDecodeError, OSError) as e:
        return {
            "success": False,
            "message": f"Could not read template '{safe_name}': {str(e)[:100]}",
            "error": str(e),
        }


def _tool_create_template(args: dict) -> dict:
    """
    WHAT DOES THIS FUNCTION DO?
    Creates a new AI pipeline template file.
    The LLM provides the template name and the stages list.
    """
    status_code, result = api_create_template(args)
    if status_code == 200:
        return {
            "success": True,
            "message": f"Created template: '{result['name']}'",
            "data": result,
        }
    return {
        "success": False,
        "message": f"Failed to create template: {result.get('error', 'Unknown error')}",
        "error": result.get("error"),
    }


def _tool_delete_template(args: dict) -> dict:
    """
    WHAT DOES THIS FUNCTION DO?
    Deletes a template file by name.
    """
    template_name = args.get("template_name")
    if not template_name:
        return {
            "success": False,
            "message": "Cannot delete template: missing 'template_name' argument",
            "error": "template_name is required",
        }

    status_code, result = api_delete_template(template_name)
    if status_code == 200:
        return {
            "success": True,
            "message": f"Deleted template: '{result['name']}'",
        }
    return {
        "success": False,
        "message": f"Failed to delete template: {result.get('error', 'Unknown error')}",
        "error": result.get("error"),
    }


async def _tool_run_task(args: dict, username: str) -> dict:
    """
    WHAT DOES THIS FUNCTION DO?
    Runs a task through the AI orchestrator pipeline.

    This is the most complex tool because it:
    1. Finds the task by ID in the user's data
    2. Builds a prompt file from the task data
    3. Runs the full multi-stage LLM pipeline
    4. Returns the result (success/failure + output info)

    Note: This function is async because the orchestrator takes time.
    """
    task_id = args.get("task_id")
    if task_id is None:
        return {
            "success": False,
            "message": "Cannot run task: missing 'task_id' argument",
            "error": "task_id is required",
        }

    # Step 1: Find the task in the user's data
    data = load_user_data(username)
    tasks = data.get("tasks", [])
    matching_tasks = [t for t in tasks if t.get("id") == int(task_id)]

    if not matching_tasks:
        return {
            "success": False,
            "message": f"Task {task_id} not found",
            "error": f"task_id {task_id} does not exist",
        }

    task = matching_tasks[0]

    # Step 2: Build the process payload (same format as the "Process (AI)" button sends)
    process_payload = {
        "title": task.get("title", "Untitled"),
        "body": task.get("content", ""),
        "priority": task.get("priority", "medium"),
        "template": args.get("template") or task.get("template", "freeform"),
        "date": task.get("date", ""),
    }

    # Step 3: Run the orchestrator
    try:
        status_code, result = await api_process(process_payload)
        if status_code == 200:
            return {
                "success": True,
                "message": f"Task '{task['title']}' processed. Status: {result.get('status', 'unknown')}. Word count: {result.get('word_count', 0)}.",
                "data": result,
            }
        return {
            "success": False,
            "message": f"Task '{task['title']}' failed: {result.get('error', 'Unknown error')}",
            "error": result.get("error"),
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Task '{task['title']}' failed with exception: {str(e)[:200]}",
            "error": str(e),
        }


# ── ENHANCED SYSTEM PROMPT ─────────────────────────────────────
#
# WHAT IS THIS?
# The system prompt is the first message sent to the LLM. It tells
# the LLM who it is, what it can do, and how to behave.
#
# We include TOOL_DEFINITIONS_TEXT here so the LLM knows what tools
# are available and how to use them.

def build_system_prompt() -> str:
    """
    WHAT DOES THIS FUNCTION DO?
    Builds the complete system prompt by combining:
    1. The base description of the application
    2. The knowledge base (roles, templates, how things work)
    3. The tool definitions

    This ensures the LLM has accurate knowledge of what exists in the system
    so it doesn't hallucinate templates, roles, or features.
    """
    base_prompt = (
        "You are the AI assistant for timemanager — a local autonomous task "
        "orchestration engine.\n\n"
        "Tasks have: title, description, priority, date, and an AI template. "
        "Templates are JSON schematics defining multi-stage LLM pipelines "
        "(planner → generator → verifier, etc.). Each stage has a role, prompt "
        "template with {{placeholders}}, and inputs from prior stages.\n\n"
        "On \"Process (AI)\": backend picks a template, builds a prompt with "
        "frontmatter, executes the schematic across providers (OpenRouter, Groq, "
        "Cerebras, NVIDIA) via a slot scheduler (rate limits, cooldowns, fallback), "
        "and writes output to backend/outputs/."
    )

    # ── KNOWLEDGE BASE ─────────────────────────────────────────
    # This section gives the LLM accurate knowledge about the system.
    # It prevents hallucination of roles, templates, and features.

    knowledge_base = """

═══════════════════════════════════════════════════════════
SYSTEM KNOWLEDGE — READ CAREFULLY
═══════════════════════════════════════════════════════════

VALID ROLES (only these 7 exist — do NOT invent others):
- planner: Emits structured JSON sub-schematics for nested planning (outputs JSON arrays/objects)
- parser: Extracts structured JSON from messy text (outputs a single JSON object)
- critiquer: Reviews content, emits a bullet list of actionable issues (MUST use bullets, never says "looks fine")
- verifier: Returns {"pass": bool, "reason": str} for one pass/fail criterion
- generator: Produces new content (the main content-creation role, writes prose/markdown)
- transformer: Takes existing content + directive, returns the FULL revised content
- assembler: Deterministic string stitching. NO LLM call. Uses {placeholder} templates to concatenate stage outputs.

HOW TEMPLATES WORK:
- A template has: task_type (string label), task (short description), stages (list), output_rules (optional)
- Each stage has: name (unique string identifier), role (MUST be one of the 7 above), instructions (the prompt/directive text)
- Stages can optionally have: inputs (list of context paths), max_tokens (int), fanout ({"over": "context_path", "max_parallel": int})
- Stages execute SEQUENTIALLY. Each stage's output is stored in a shared context dictionary.
- Fanout: When a stage has fanout.over, it runs once per item in that list (e.g., per topic). Results stored as a list.
- Input paths use dot notation: "stage_name" or "stage_name.field" or "stage_name.{i}" (fanout index) or "stage_name.*" (all items joined)
- The orchestrator rotates AI providers between stages to avoid rate limits and distribute load.

HOW PRIORITY AND DATE WORK:
- Priority: "high", "medium", or "low" — affects task sorting and scheduling order in the UI
- Date: YYYY-MM-DD format — the task's target/completion date
- These are METADATA fields on the task object. Do NOT write about them in task descriptions or content.
- When creating a task, set these as fields in create_task args, not as text inside the description.

HOW TO RECOMMEND TEMPLATES:
1. Use list_templates to see what actually exists — each template returns a description explaining what it does
2. Use get_template to see the full stage structure before recommending a template
3. Only recommend templates that actually exist in the system
4. If no template fits, suggest creating a new one with create_template
5. NEVER invent template names, stage names, or role names

AVAILABLE TEMPLATES (current — verify with list_templates at runtime):
- freeform: Single-stage generator. Best for simple tasks: write an email, draft text, produce markdown.
- structured_lesson: 5-stage pipeline with objectives, quality check, transform. Most thorough lesson template.
- research_paper: 10-stage pipeline with URL discovery, trafilatura fetch, grounded citations. Best for research papers.

CREATING TASKS WITH DESCRIPTION AND TEMPLATE:
- The "description" field (called "content" in the data model) is the task body — this is what the AI orchestrator uses as its prompt when processing the task
- Always include a detailed description when creating a task — it drives the AI's output quality
- Always specify the template when you know what type of output is needed — use list_templates to see options
- Example: create_task with title="Learn Python", description="Write a beginner lesson covering variables, loops, functions, and error handling. Include exercises and solutions.", template="lesson_plan"

RESEARCH TASKS (research_paper template):
- The research_paper template uses trafilatura>=2.0 to fetch URLs and extract readable article text
- URLs come from either: (a) seed_urls in the prompt's YAML frontmatter, or (b) the source_discovery stage auto-discovers them
- The pipeline fetches each URL, extracts article text, and injects it as fetched_sources into section_draft stages
- Section writers are FORBIDDEN from inventing citations — they must use fetched_sources only
- When creating a research task, ask the user for specific URLs they want researched, or let source_discovery auto-discover them
- If you provide URLs, include them in the task description or as seed_urls in the prompt frontmatter
"""

    return base_prompt + knowledge_base + TOOL_DEFINITIONS_TEXT


# ── THE TOOL-CALL LOOP ─────────────────────────────────────────
#
# WHAT DOES THIS FUNCTION DO?
# This is the heart of the tool-use system. It runs a loop:
#
#   Step 1: Send the conversation to the LLM
#   Step 2: Check if the LLM's response contains [TOOL_CALL: ...]
#   Step 3: If yes → execute each tool, add results to conversation, go back to Step 1
#   Step 4: If no → the LLM is giving a final answer, return it to the user
#
# The loop runs up to 5 times (MAX_TOOL_ITERATIONS) to prevent
# infinite loops if the LLM keeps generating tool calls.


# Maximum number of tool-call rounds per chat message
# This prevents the LLM from getting stuck in an infinite loop
MAX_TOOL_ITERATIONS = 5


async def call_llm_once(
    messages: list[dict],
    scheduler,
    client,
    max_tokens: int = 4096,
) -> str:
    """
    WHAT DOES THIS FUNCTION DO?
    Makes a single call to the LLM provider and returns the response text.

    Arguments:
    - messages: The conversation history (list of {role, content} dicts)
    - scheduler: The slot scheduler (picks which AI provider to use)
    - client: The HTTP client for making API calls
    - max_tokens: Maximum length of the LLM's response

    Returns the LLM's response as a string.
    """
    from scheduler import call_slot
    from content.roles import Roles

    # Pick the best available AI provider for this request
    picked_slot = scheduler.pick_slot(Roles("generator"))

    # Make the API call to the chosen provider
    response_text = await call_slot(client, picked_slot, messages, max_tokens=max_tokens)

    return response_text, picked_slot


async def api_chat(payload: dict, username: str = None) -> tuple[int, dict]:
    """
    WHAT DOES THIS FUNCTION DO?
    Handles a chat message with full tool-use support.

    The flow:
    1. Get the user's message from the conversation history
    2. Add the system prompt (which includes tool definitions)
    3. Call the LLM
    4. Check if the response has tool calls
    5. If yes → execute them, feed results back to LLM, repeat
    6. If no → return the LLM's response to the user

    Arguments:
    - payload: { "messages": [...conversation history...], "username": "user@email.com" }

    Returns:
    {
        "response": "The LLM's final answer",
        "tool_calls_made": [list of tools that were executed],
        "provider": "which AI provider was used",
        "model": "which model was used"
    }
    """
    # Step 1: Get the conversation history
    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        return 400, {"error": "messages must be an array"}

    # Step 2: Get the username (so we know whose data to modify)
    # This comes from the client payload or falls back to a default
    effective_username = payload.get("username") or username or "default_user"

    try:
        # Import the modules we need for calling the LLM
        import providers
        from scheduler import SlotScheduler, call_slot
        from content.roles import Roles
        import httpx

        # Step 3: Set up the AI provider and slot scheduler
        provs = providers.make_provider_registry()
        slots = providers.make_slot_registry(provs)
        if not slots:
            return 500, {"error": "no LLM slots configured — check .env for API keys"}

        scheduler = SlotScheduler(slots)

        # Step 4: Build the full message list (system prompt + conversation)
        system_prompt = build_system_prompt()
        full_messages = [{"role": "system", "content": system_prompt}] + list(messages)

        # Step 5: Create an HTTP client for API calls
        # Timeout is 120 seconds per LLM call (generous for large responses)
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Step 6: Run the tool-call loop
            # We track all tool calls that were made to report back to the client
            all_tool_calls_made = []
            last_provider_info = None

            for iteration in range(MAX_TOOL_ITERATIONS):
                # Call the LLM with the full conversation so far
                response_text, picked_slot = await call_llm_once(
                    messages=full_messages,
                    scheduler=scheduler,
                    client=client,
                    max_tokens=4096,  # Generous limit so the LLM can write long responses
                )

                last_provider_info = {
                    "provider": picked_slot.provider.name,
                    "model": picked_slot.model,
                }

                # Check if the response contains tool calls
                tool_calls = parse_tool_calls(response_text)

                if not tool_calls:
                    # No tool calls → this is the final answer for the user
                    return 200, {
                        "response": response_text,
                        "tool_calls_made": all_tool_calls_made,
                        "provider": last_provider_info["provider"],
                        "model": last_provider_info["model"],
                    }

                # Step 7: Execute each tool call
                # The LLM might request multiple tools in one response
                tool_results_text = []

                for tool_call in tool_calls:
                    tool_name = tool_call["tool"]
                    tool_args = tool_call["args"]

                    # Execute the tool and get the result
                    if tool_name == "run_task":
                        # run_task is async, so we await it
                        result = await execute_tool(tool_name, tool_args, effective_username)
                    else:
                        # Other tools are sync (instant)
                        result = execute_tool(tool_name, tool_args, effective_username)

                    # Track this tool call for reporting
                    all_tool_calls_made.append({
                        "tool": tool_name,
                        "args": tool_args,
                        "success": result.get("success", False),
                        "message": result.get("message", ""),
                    })

                    # Build a result message to send back to the LLM
                    # The LLM needs to know whether the tool succeeded and what happened
                    result_text = f"[TOOL_RESULT: {tool_name}] success={result['success']}. {result['message']}"
                    if result.get("data") is not None:
                        result_text += "\n" + str(result["data"])
                    tool_results_text.append(result_text)

                # Step 8: Add the LLM's response + tool results to the conversation
                # This way the LLM sees what happened and can continue
                full_messages.append({"role": "assistant", "content": response_text})

                # Combine all tool results into one message
                combined_results = "\n".join(tool_results_text)
                full_messages.append({
                    "role": "user",
                    "content": f"Tool execution results:\n{combined_results}\n\nNow continue the conversation. If all tools succeeded and you have nothing more to do, give the user a summary of what was done.",
                })

            # Step 9: If we hit the iteration limit, force a final response
            # The LLM might still be generating tool calls — we need to stop
            # and get a final answer for the user
            full_messages.append({
                "role": "user",
                "content": "You have reached the maximum number of tool calls. Stop using tools and give the user a final summary of what was accomplished.",
            })

            final_response, picked_slot = await call_llm_once(
                messages=full_messages,
                scheduler=scheduler,
                client=client,
                max_tokens=2048,
            )

            return 200, {
                "response": final_response,
                "tool_calls_made": all_tool_calls_made,
                "provider": picked_slot.provider.name,
                "model": picked_slot.model,
            }

    except Exception as e:
        err_msg = str(e)
        return 500, {"error": f"chat failed: {err_msg[:300]}"}


# ═══════════════════════════════════════════════════════════════
# SECTION 8: API ENDPOINTS — SETTINGS (API keys)
# ═══════════════════════════════════════════════════════════════
#
# WHAT DOES THIS SECTION DO?
# Manages API keys stored in backend/.env. The UI lets users
# enter their API keys for different AI providers, and these
# endpoints read and write them securely.


def api_get_keys() -> tuple[int, dict]:
    """Reads API keys from .env and returns masked versions (showing only first/last few chars)"""
    envfile = BACKEND / ".env"
    if not envfile.exists():
        return 500, {"error": ".env file not found"}

    env_lines = envfile.read_text(encoding="utf-8").splitlines()
    provider_env_map = {
        "openrouter": ("OPENROUTER_API_KEY", None),
        "groq": ("GROQ_API_KEY", None),
        "cerebras": ("CEREBRAS_API_KEY", None),
        "nvidia": ("NVIDIA_API_KEY", None),
    }

    for line in env_lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        for name, (env_key, _) in provider_env_map.items():
            if key == env_key and value:
                provider_env_map[name] = (env_key, value)

    result = {}
    for name, (_, value) in provider_env_map.items():
        if value:
            if len(value) > 16:
                masked = value[:10] + "********" + value[-4:]
            else:
                masked = value[:6] + "****" + value[-2:]
            result[name] = {"configured": True, "masked": masked}
        else:
            result[name] = {"configured": False, "masked": ""}

    return 200, result


def api_save_key(payload: dict) -> tuple[int, dict]:
    """Saves an API key to .env for a specific provider"""
    provider = (payload.get("provider") or "").strip().lower()
    key = (payload.get("key") or "").strip()

    env_map = {
        "openrouter": "OPENROUTER_API_KEY",
        "groq": "GROQ_API_KEY",
        "cerebras": "CEREBRAS_API_KEY",
        "nvidia": "NVIDIA_API_KEY",
    }

    if provider not in env_map:
        return 400, {"error": f"unknown provider: {provider}"}
    if not key:
        return 400, {"error": "key is required"}

    env_key = env_map[provider]
    envfile = BACKEND / ".env"
    if not envfile.exists():
        return 500, {"error": ".env file not found"}

    lines = envfile.read_text(encoding="utf-8").splitlines()
    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        env_name, _ = stripped.split("=", 1)
        if env_name.strip() == env_key:
            new_lines.append(f"{env_key}={key}")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"{env_key}={key}")

    # Atomic write: write to temp file, then rename
    tmpfile = envfile.with_suffix(".tmp")
    tmpfile.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    tmpfile.replace(envfile)

    if len(key) > 16:
        masked = key[:10] + "********" + key[-4:]
    else:
        masked = key[:6] + "****" + key[-2:]

    return 200, {"ok": True, "masked": masked, "provider": provider}


# ═══════════════════════════════════════════════════════════════
# SECTION 9: HTTP REQUEST HANDLER (routes incoming requests)
# ═══════════════════════════════════════════════════════════════
#
# WHAT DOES THIS SECTION DO?
# This is the traffic cop of the server. Every HTTP request from
# the browser comes through here, and this class decides which
# function should handle it.
#
# Example:
# - GET /api/tasks → calls api_list_tasks()
# - POST /api/tasks → reads the JSON body and calls api_create_task()
# - DELETE /api/templates/my_template → calls api_delete_template()


class Handler(SimpleHTTPRequestHandler):
    """
    WHAT IS THIS CLASS?
    This handles every incoming HTTP request. It has methods for:
    - do_GET: Handles GET requests (reading data)
    - do_POST: Handles POST requests (creating/updating data)
    - do_DELETE: Handles DELETE requests (removing data)
    - do_PUT: Handles PUT requests (updating data)
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        """Add cache-control headers to static files so browsers always get fresh copies"""
        if self.path.endswith((".css", ".js", ".html")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        """Prints each request to the console for debugging"""
        print(f"[server] {fmt % args}", flush=True)

    # ── GET routing (reading data) ─────────────────────────────

    def do_GET(self):
        """
        WHAT DOES THIS METHOD DO?
        Handles all GET requests. It looks at the URL path and
        calls the appropriate function.

        Example URLs:
        GET /api/templates → list all templates
        GET /api/tasks → list all tasks for the logged-in user
        GET /api/outputs → list all output files
        GET /api/log → get the orchestrator log
        """
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        # GET /api/templates — list available templates
        if path == "/api/templates":
            code, body = api_list_templates()
            self._json(code, body)
            return

        # GET /api/tasks — list all tasks for this user
        if path == "/api/tasks":
            username = _get_current_username_from_request(self)
            code, body = api_list_tasks(username)
            self._json(code, body)
            return

        # GET /api/outputs/some_file — read a specific output file
        if path.startswith("/api/outputs/"):
            name = path[len("/api/outputs/"):]
            code, body = api_get_output(unquote(name))
            self._json(code, body)
            return

        # GET /api/outputs — list all output files
        if path == "/api/outputs":
            code, body = api_list_outputs()
            self._json(code, body)
            return

        # GET /api/settings/keys — read API key status
        if path == "/api/settings/keys":
            code, body = api_get_keys()
            self._json(code, body)
            return

        # GET /api/log — read the orchestrator event log
        if path == "/api/log":
            code, body = api_get_log()
            self._json(code, body)
            return

        # If no API route matched, serve as a static file (HTML, CSS, JS, etc.)
        super().do_GET()

    # ── POST routing (creating/sending data) ────────────────────

    def do_POST(self):
        """
        WHAT DOES THIS METHOD DO?
        Handles all POST requests. POST means the browser is sending
        data to the server (like creating a task or sending a chat message).

        Example URLs:
        POST /api/tasks → create a new task
        POST /api/process → run the orchestrator on a task
        POST /api/chat → send a chat message (with tool-use)
        POST /api/templates → create a new template
        """
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        # Helper: read the JSON body from the request
        def read_json_body():
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length)
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid JSON body"})
                return None

        # POST /api/settings/keys — save an API key
        if path == "/api/settings/keys":
            payload = read_json_body()
            if payload is None:
                return
            code, body = api_save_key(payload)
            self._json(code, body)
            return

        # POST /api/tasks — create a new task
        if path == "/api/tasks":
            username = _get_current_username_from_request(self)
            payload = read_json_body()
            if payload is None:
                return
            code, body = api_create_task(username, payload)
            self._json(code, body)
            return

        # POST /api/process — run the orchestrator on a task
        if path == "/api/process":
            payload = read_json_body()
            if payload is None:
                return
            try:
                code, body = asyncio.run(api_process(payload))
            except Exception as e:
                code, body = 500, {"error": f"orchestrator failed: {str(e)[:300]}"}
            self._json(code, body)
            return

        # POST /api/chat — send a chat message (with tool-use)
        if path == "/api/chat":
            username = _get_current_username_from_request(self)
            payload = read_json_body()
            if payload is None:
                return
            # Add the username to the payload so api_chat knows whose data to modify
            payload["username"] = username
            try:
                code, body = asyncio.run(api_chat(payload, username))
            except Exception as e:
                code, body = 500, {"error": f"chat failed: {str(e)[:300]}"}
            self._json(code, body)
            return

        # POST /api/templates — create a new template
        if path == "/api/templates":
            payload = read_json_body()
            if payload is None:
                return
            code, body = api_create_template(payload)
            self._json(code, body)
            return

        self.send_error(404, "Not Found")

    # ── PUT routing (updating existing data) ────────────────────

    def do_PUT(self):
        """
        WHAT DOES THIS METHOD DO?
        Handles PUT requests, which are used to update existing resources.

        Example URL:
        PUT /api/tasks/1234567890 — update the task with ID 1234567890

        The URL path contains the resource type and ID.
        """
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        # Helper: read the JSON body
        def read_json_body():
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length)
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid JSON body"})
                return None

        # PUT /api/tasks/<task_id> — update a specific task
        if path.startswith("/api/tasks/"):
            # Extract the task ID from the URL (everything after /api/tasks/)
            try:
                task_id = int(path.split("/api/tasks/")[1])
            except (ValueError, IndexError):
                self._json(400, {"error": "invalid task ID"})
                return

            username = _get_current_username_from_request(self)
            payload = read_json_body()
            if payload is None:
                return

            code, body = api_update_task(username, task_id, payload)
            self._json(code, body)
            return

        self.send_error(404, "Not Found")

    # ── DELETE routing (removing data) ─────────────────────────

    def do_DELETE(self):
        """
        WHAT DOES THIS METHOD DO?
        Handles DELETE requests, which are used to remove resources.

        Example URLs:
        DELETE /api/log — clear the orchestrator log
        DELETE /api/tasks/1234567890 — delete a specific task
        DELETE /api/templates/my_template — delete a template
        """
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        # DELETE /api/log — clear the orchestrator event log
        if path == "/api/log":
            code, body = api_clear_log()
            self._json(code, body)
            return

        # DELETE /api/tasks/<task_id> — delete a specific task
        if path.startswith("/api/tasks/"):
            try:
                task_id = int(path.split("/api/tasks/")[1])
            except (ValueError, IndexError):
                self._json(400, {"error": "invalid task ID"})
                return

            username = _get_current_username_from_request(self)
            code, body = api_delete_task(username, task_id)
            self._json(code, body)
            return

        # DELETE /api/templates/<template_name> — delete a template
        if path.startswith("/api/templates/"):
            template_name = path.split("/api/templates/")[1]
            code, body = api_delete_template(template_name)
            self._json(code, body)
            return

        self.send_error(404, "Not Found")

    # ── Helper methods ──────────────────────────────────────────

    def _json(self, code: int, body: dict) -> None:
        """
        WHAT DOES THIS METHOD DO?
        Sends a JSON response back to the browser.

        It:
        1. Converts the Python dictionary to a JSON string
        2. Sends the HTTP status code (200 = OK, 404 = Not Found, etc.)
        3. Sets the Content-Type header so the browser knows it's JSON
        4. Writes the JSON data to the response
        """
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):
        """Suppresses default error pages for SPA-like routing"""
        if code == 404:
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"404 Not Found")
        else:
            super().send_error(code, message, explain)


# ═══════════════════════════════════════════════════════════════
# SECTION 10: SERVER STARTUP
# ═══════════════════════════════════════════════════════════════
#
# WHAT DOES THIS SECTION DO?
# This runs when you execute "python server.py" or "run.bat".
# It starts the HTTP server on port 8080 and begins listening
# for incoming requests from the browser.


def main():
    """
    WHAT DOES THIS FUNCTION DO?
    Starts the web server and keeps it running until you press Ctrl+C.
    """
    port = 8080

    # ThreadingHTTPServer handles multiple requests at the same time.
    # This means the log polling works while a task is being processed.
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    print(f"timemanager server -> http://127.0.0.1:{port}", flush=True)
    print(f"templates dir      -> {TEMPLATES_DIR}", flush=True)
    print(f"prompts dir        -> {PROMPTS_DIR}", flush=True)
    print(f"data dir           -> {DATA_DIR}", flush=True)
    print("Ctrl+C to stop", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
