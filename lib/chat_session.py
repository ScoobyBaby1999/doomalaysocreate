"""New simplified chat session manager.

ONE class per chat session. The Strands Agent's callback handler is the
SINGLE source of events. NO post-turn walk, NO thinking merge, NO cursor
tracking. Dead simple.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from oplog import log_event


# Session registry (thread-safe)
_sessions: dict[str, "ChatSession"] = {}
_sessions_lock = threading.Lock()
MAX_SESSIONS = 64
SESSION_TTL_S = 7200  # 2 hours idle


def get_session(sid: str) -> "ChatSession | None":
    return _sessions.get(sid)


def get_or_create(chat_session_id: str, model: str | None = None,
                  workspace_path: Path | None = None,
                  effort: str | None = None,
                  web_search: bool = False,
                  deep_research: bool = False,
                  web_search_template: str | None = None,
                  deep_research_mode: str | None = None,
                  deep_research_template: str | None = None,
                  panel=None) -> "ChatSession":
    """Get an existing session by chat_session_id, or create a new one."""
    with _sessions_lock:
        # Sweep expired sessions
        now = time.time()
        expired = [sid for sid, s in _sessions.items()
                   if now - s.updated > SESSION_TTL_S or s.closed]
        for sid in expired:
            old = _sessions.pop(sid, None)
            if old:
                try:
                    old.close()
                except Exception:
                    pass

        # Reuse existing session linked to this chat_session_id
        for s in _sessions.values():
            if s.chat_session_id == chat_session_id:
                # Check if model changed — if so, close old + create new
                if model and s.model and _normalize_model(s.model) != _normalize_model(model):
                    s.close()
                    _sessions.pop(s.id, None)
                    break
                return s

        # Evict oldest idle session if pool is full
        if len(_sessions) >= MAX_SESSIONS:
            oldest = None
            for s in _sessions.values():
                if s.status not in ("running", "starting") and not s.closed:
                    if oldest is None or s.updated < oldest.updated:
                        oldest = s
            if oldest:
                oldest.close()
                _sessions.pop(oldest.id, None)

        # Create new session
        s = ChatSession(
            chat_session_id=chat_session_id,
            model=model,
            workspace_path=workspace_path,
            effort=effort,
            web_search=web_search,
            deep_research=deep_research,
            web_search_template=web_search_template,
            deep_research_mode=deep_research_mode,
            deep_research_template=deep_research_template,
            panel=panel,
        )
        _sessions[s.id] = s
        return s


def _normalize_model(model: str | None) -> str | None:
    if not model:
        return None
    return model.replace("openai/", "").split("/")[-1].replace(":free", "").lower()


class ChatSession:
    """One per chat session. Manages the Strands Agent + event queue.

    The callback handler is the SINGLE source of events. No post-turn walk.
    """

    def __init__(self, chat_session_id: str, model: str | None = None,
                 workspace_path: Path | None = None,
                 effort: str | None = None,
                 web_search: bool = False,
                 deep_research: bool = False,
                 web_search_template: str | None = None,
                 deep_research_mode: str | None = None,
                 deep_research_template: str | None = None,
                 panel=None):
        self.id = uuid.uuid4().hex[:16]
        self.chat_session_id = chat_session_id
        self.model = model
        self.workspace_path = workspace_path or Path("/tmp/agent")
        self.workspace_path.mkdir(parents=True, exist_ok=True)
        self.effort = effort
        self.web_search = web_search
        self.deep_research = deep_research
        self.web_search_template = web_search_template
        self.deep_research_mode = deep_research_mode
        self.deep_research_template = deep_research_template
        self.panel = panel

        self.events: list[dict] = []
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._agent = None
        self._adapter = None
        self.status = "idle"
        self.closed = False
        self.created = time.time()
        self.updated = self.created
        self._assistant_emitted = False
        self._thread = None
        self._turn_start_idx = 0  # Index in self.events where the current turn starts

    def send(self, message: str) -> None:
        """Send a message. Runs the agent in a background thread."""
        self.updated = time.time()
        self.status = "running"
        self._assistant_emitted = False
        self._turn_start_idx = len(self.events)

        # Emit user event
        self._emit({"type": "user", "text": message})

        # Run in background thread (non-blocking for the HTTP handler)
        self._thread = threading.Thread(target=self._run_turn, args=(message,), daemon=True)
        self._thread.start()

    def _run_turn(self, message: str) -> None:
        """Run a single agent turn. Called in a background thread."""
        try:
            if self._agent is None:
                self._create_agent()

            log_event("chat_turn_start", session_id=self.id,
                       chat_session_id=self.chat_session_id, model=self.model)

            # The agent call is blocking — the callback handler emits events
            # in real-time as the model generates tokens.
            self._agent(message)

            log_event("chat_turn_done", session_id=self.id)
        except Exception as e:
            log_event("chat_turn_error", session_id=self.id, error=str(e)[:300])
            self._emit({"type": "error", "error": str(e)[:300]})
        finally:
            # Check if any assistant text was emitted
            if not self._assistant_emitted:
                self._emit({"type": "assistant",
                           "text": "(no response from the model)"})
            self.status = "idle"
            self._emit({"type": "status", "state": "idle"})
            self.updated = time.time()

    def _create_agent(self) -> None:
        """Create the Strands Agent with proper configuration."""
        from strands import Agent
        from strands.models.litellm import LiteLLMModel
        from strands.agent.conversation_manager import SlidingWindowConversationManager
        
        # Resolve model + provider
        model_id, api_key, api_base, extra_headers, provider_name = self._resolve_model()

        # Build effort body
        extra_body = self._build_effort_body(provider_name, model_id)

        # Build system prompt with model identity
        model_display = model_id.replace("openai/", "").split("/")[-1]
        provider_display = provider_name.replace("_API_KEY", "").replace("_TOKEN", "").replace("_", " ").title()
        system_prompt = (
            "You are doomalaysocreate's agent — an agentic orchestrator running inside "
            "the user's own private Space container. Your workspace directory is your sandbox. "
            "Be direct and concise; lead with outcomes.\n\n"
            f"You are running as {model_display} via {provider_display}. "
            f"If the user asks which model you are, tell them you are {model_display}."
        )

        # Build LLM
        client_args = {"api_key": api_key, "timeout": 120, "num_retries": 3}
        if api_base:
            client_args["api_base"] = api_base
        if extra_headers:
            client_args["extra_headers"] = extra_headers

        llm_kwargs = dict(client_args=client_args, model_id=model_id, stream=True)
        if extra_body:
            llm_kwargs["additional_request_params"] = {"extra_body": extra_body}

        llm = LiteLLMModel(**llm_kwargs)

        # Build tools
        tools = self._build_tools()

        # Build conversation manager — CRITICAL: per_turn=True
        conv_manager = SlidingWindowConversationManager(
            window_size=40,
            should_truncate_results=True,
            per_turn=True,  # Apply management BEFORE every model call
            proactive_compression=True,  # Auto-compress when 70% context used
        )

        # Create the agent — NO FileSessionManager (it replays old conversations
        # from /data which causes the agent to re-generate previous responses).
        # The SlidingWindowConversationManager handles in-memory context.
        self._agent = Agent(
            model=llm,
            tools=tools,
            system_prompt=system_prompt,
            callback_handler=self._callback,
            conversation_manager=conv_manager,
        )

        log_event("chat_agent_created", session_id=self.id,
                   model=model_id, provider=provider_name,
                   tools_count=len(tools))

    def _callback(self, **kwargs: Any) -> None:
        """THE single source of events. Called by Strands for each token/chunk.

        SP11.2: Handle BOTH thinking styles:
        - Accumulated: reasoningText grows each call (new starts with old) → emit 'thinking' (replace)
        - Fragment: reasoningText is just the new chunk → emit 'thinking_delta' (append)
        """
        reasoningText = kwargs.get("reasoningText")
        data = kwargs.get("data")
        complete = kwargs.get("complete", False)
        event = kwargs.get("event", {})
        tool_use = event.get("contentBlockStart", {}).get("start", {}).get("toolUse")

        if reasoningText:
            # SP11.2: Detect accumulated vs fragment style
            old_thinking = getattr(self, "_last_reasoning", "")
            if old_thinking and reasoningText.startswith(old_thinking) and len(reasoningText) > len(old_thinking):
                # Accumulated style — new text includes old text → REPLACE
                self._emit({"type": "thinking", "text": reasoningText})
            elif old_thinking and old_thinking.startswith(reasoningText):
                # Stale re-emit of a shorter prefix → SKIP
                pass
            else:
                # Fragment style — new text is a separate chunk → APPEND
                self._emit({"type": "thinking_delta", "text": reasoningText})
            self._last_reasoning = reasoningText

        if data:
            self._assistant_emitted = True
            self._emit({"type": "assistant_delta", "text": data})

        if tool_use:
            tool_name = tool_use.get("name", "tool")
            tool_input = tool_use.get("input", {})
            self._emit({"type": "tool_use", "name": tool_name,
                       "summary": str(tool_input)[:200],
                       "text": json.dumps(tool_input, default=str)[:2000]})

        # Capture tool results
        tool_result = event.get("contentBlockDelta", {}).get("toolResult")
        if tool_result:
            result_text = ""
            content_blocks = tool_result.get("content", [])
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if isinstance(block, dict) and "text" in block:
                        result_text += block["text"]
            elif isinstance(content_blocks, str):
                result_text = content_blocks
            self._emit({"type": "tool_result", "text": result_text[:2000],
                       "is_error": tool_result.get("status") == "error"})

        if complete:
            self._emit({"type": "assistant_complete"})

    def _emit(self, event: dict) -> None:
        """Add event to the list + push to all SSE subscribers."""
        with self._lock:
            event["i"] = len(self.events)
            event["ts"] = time.time()
            self.events.append(event)
            subs = list(self._subscribers)

        self.updated = time.time()
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # Drop if subscriber is too slow

    def subscribe(self, since: int = 0) -> queue.Queue:
        """Subscribe to live events. Returns a Queue."""
        with self._lock:
            q: queue.Queue = queue.Queue(maxsize=512)
            # Replay events from `since`
            start = self._turn_start_idx if since == 0 else max(0, since)
            for ev in self.events[start:]:
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    break
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def snapshot(self, since: int = 0) -> dict:
        """Get a snapshot of events for polling.
        
        If since=0, returns only events from the CURRENT turn (not all history).
        If since>0, returns events from that index (for catching up).
        """
        with self._lock:
            if since == 0:
                # Return only current turn's events (user + assistant + tools)
                start = self._turn_start_idx
            else:
                start = max(0, since)
            events = self.events[start:]
            return {
                "session_id": self.id,
                "status": self.status,
                "events": events,
                "next": len(self.events),
            }

    def interrupt(self) -> None:
        """Interrupt the current turn."""
        if self._agent:
            canceler = getattr(self._agent, "cancel", None)
            if callable(canceler):
                try:
                    canceler()
                except Exception:
                    pass
        self.status = "idle"
        self._emit({"type": "status", "state": "idle", "detail": "interrupted"})

    def close(self) -> None:
        self.closed = True
        if self._agent:
            cleanup = getattr(self._agent, "cleanup", None)
            if callable(cleanup):
                try:
                    cleanup()
                except Exception:
                    pass

    def _resolve_model(self) -> tuple[str, str, str | None, dict | None, str]:
        """Resolve model + provider. Returns (model_id, api_key, api_base, extra_headers, provider_name)."""
        if not self.model:
            # Pick a default
            return self._pick_default_model()

        # Try to resolve through _resolve_open_model
        try:
            from agent_sessions import _resolve_open_model
            pair = _resolve_open_model(self.model)
            if pair:
                model, base_url, key_env, provider_label, extra_headers = pair
                api_key = os.environ.get(key_env, "")
                if not api_key:
                    raise RuntimeError(f"no API key for {key_env}")
                return model, api_key, base_url, extra_headers, provider_label or key_env
        except Exception as e:
            log_event("chat_model_resolve_error", error=str(e)[:200])

        # Fallback: pick default
        return self._pick_default_model()

    def _pick_default_model(self) -> tuple[str, str, str | None, dict | None, str]:
        """Pick a known-good default model from the first available provider."""
        from agent_sessions import _pick_open_llm
        picked = _pick_open_llm()
        if picked is None:
            raise RuntimeError("no open-tier provider key available")
        key_env, model, base_url = picked
        api_key = os.environ.get(key_env, "")
        return model, api_key, base_url, None, key_env

    def _build_effort_body(self, provider: str, model: str) -> dict:
        """Build the effort/reasoning body for the provider."""
        if not self.effort or self.effort in ("low", "off"):
            return {}

        try:
            from effort_detector import detect_effort_body
            body = detect_effort_body(provider, model, self.effort)
            if body:
                return body
        except Exception:
            pass

        # Fallback to old catalog
        try:
            from provider_tools import reasoning_body_for
            return reasoning_body_for(provider, model)
        except Exception:
            return {}

    def _build_tools(self) -> list:
        """Build the tool suite for the agent."""
        tools = []
        try:
            from strands import tool as strands_tool

            @strands_tool(name="shell", description=(
                "Execute a bash command in the workspace sandbox. Returns stdout + stderr."
            ))
            def shell(command: str) -> str:
                import subprocess
                result = subprocess.run(
                    command, shell=True, capture_output=True, text=True,
                    timeout=300, cwd=str(self.workspace_path),
                    env={k: v for k, v in os.environ.items()
                         if not k.upper().endswith(("_API_KEY", "_TOKEN", "_SECRET"))}
                )
                return result.stdout + result.stderr

            tools.append(shell)
        except Exception:
            pass

        # Add more tools from strands_tools if available
        try:
            from strands_tools import file_read, file_write, editor, http_request, calculator, journal, current_time
            tools.extend([file_read, file_write, editor, http_request, calculator, journal, current_time])
        except Exception:
            pass

        # Add web search tools (DuckDuckGo, no API key needed)
        try:
            from strands import tool as strands_tool

            @strands_tool(name="web_search", description=(
                "Search the web for current information. Returns results with titles, URLs, and snippets. "
                "Use for news, documentation, facts, or any web content."
            ))
            def web_search(query: str) -> str:
                """Multi-provider web search: DDG → Wikipedia → Tavily → Brave."""
                import urllib.request, urllib.parse, re, json

                # Provider 1: DuckDuckGo HTML (free, no key)
                try:
                    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
                    req = urllib.request.Request(url, headers={
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                    })
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        html = resp.read().decode("utf-8", errors="replace")
                    results = []
                    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL):
                        u = m.group(1)
                        t = re.sub(r'<[^>]+>', '', m.group(2)).strip()
                        if u.startswith("//duckduckgo.com/l/?uddg="):
                            u = urllib.parse.unquote(u.split("uddg=")[1].split("&")[0])
                        results.append(f"[{len(results)+1}] {t}\n    {u}")
                    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)
                    for i, s in enumerate(snippets[:len(results)]):
                        c = re.sub(r'<[^>]+>', '', s).strip()
                        if i < len(results): results[i] += f"\n    {c}"
                    if results:
                        return "\n\n".join(results[:8])
                except Exception:
                    pass

                # Provider 2: Wikipedia API (free, no key, good for factual queries)
                try:
                    wurl = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(query)}&format=json&srlimit=5"
                    req = urllib.request.Request(wurl, headers={"User-Agent": "doomalaysocreate/1.0"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read())
                    wresults = []
                    for r in data.get("query", {}).get("search", []):
                        title = r.get("title", "")
                        snippet = re.sub(r'<[^>]+>', '', r.get("snippet", ""))
                        wresults.append(f"[{len(wresults)+1}] {title}\n    https://en.wikipedia.org/wiki/{urllib.parse.quote(title)}\n    {snippet}")
                    if wresults:
                        return "Wikipedia results:\n\n" + "\n\n".join(wresults)
                except Exception:
                    pass

                # Provider 3: Tavily (if API key set)
                tavily_key = os.environ.get("TAVILY_API_KEY", "")
                if tavily_key:
                    try:
                        turl = "https://api.tavily.com/search"
                        body = json.dumps({"api_key": tavily_key, "query": query, "max_results": 5}).encode()
                        req = urllib.request.Request(turl, data=body, headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            data = json.loads(resp.read())
                        tresults = []
                        for r in data.get("results", []):
                            tresults.append(f"[{len(tresults)+1}] {r.get('title','')}\n    {r.get('url','')}\n    {r.get('content','')[:200]}")
                        if tresults:
                            return "\n\n".join(tresults)
                    except Exception:
                        pass

                # Provider 4: Brave Search (if API key set)
                brave_key = os.environ.get("BRAVE_API_KEY", "")
                if brave_key:
                    try:
                        burl = f"https://api.search.brave.com/res/v1/web/search?q={urllib.parse.quote(query)}&count=5"
                        req = urllib.request.Request(burl, headers={
                            "X-Subscription-Token": brave_key,
                            "Accept": "application/json",
                        })
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            data = json.loads(resp.read())
                        bresults = []
                        for r in data.get("web", {}).get("results", []):
                            bresults.append(f"[{len(bresults)+1}] {r.get('title','')}\n    {r.get('url','')}\n    {r.get('description','')[:200]}")
                        if bresults:
                            return "\n\n".join(bresults)
                    except Exception:
                        pass

                return f"No web search results found for: {query}. The search providers may be rate-limited. Try using web_fetch to read a specific URL."

            @strands_tool(name="web_fetch", description=(
                "Fetch and read the content of a web page. Returns the text content of the page. "
                "Use this after web_search to read specific pages."
            ))
            def web_fetch(url: str) -> str:
                """Fetch a web page and return its text content."""
                import urllib.request
                import re
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; doomalaysocreate-agent/1.0)"
                })
                with urllib.request.urlopen(req, timeout=15) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
                # Strip HTML tags
                text = re.sub(r'<script[\s\S]*?</script>', '', html)
                text = re.sub(r'<style[\s\S]*?</style>', '', text)
                text = re.sub(r'<[^>]+>', ' ', text)
                text = re.sub(r'\s+', ' ', text).strip()
                return text[:10000]  # Cap at 10k chars

            tools.extend([web_search, web_fetch])
        except Exception as e:
            log_event("web_search_tool_error", error=str(e)[:200])

        # Add think tool (for reasoning)
        try:
            from strands_tools import think
            tools.append(think)
        except Exception:
            pass

        return tools
