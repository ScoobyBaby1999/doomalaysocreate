/**
 * V2 Chat API client — simplified, SSE-only, no polling.
 * 
 * Usage:
 *   const client = new V2ChatClient(baseUrl, getToken);
 *   const session = await client.createSession({ model: "openai/z-ai/glm-5.2" });
 *   const { session_id } = await client.send(session.id, "Hi", { model: "openai/z-ai/glm-5.2" });
 *   const ac = client.stream(session_id, (ev) => console.log(ev), (err) => console.error(err), () => console.log("done"));
 *   // Later: ac.abort() to cancel
 */
export interface V2AgentEvent {
  i: number;
  ts: number;
  type: "user" | "assistant" | "assistant_delta" | "assistant_complete" | "thinking" | "tool_use" | "tool_result" | "status" | "error" | "title";
  text?: string;
  state?: string;
  detail?: string;
  name?: string;
  summary?: string;
  error?: string;
  title?: string;
  sources?: any[];
  usage?: { input_tokens: number; output_tokens: number; total_tokens: number };
  cost_usd?: number | null;
}

export interface V2ChatSession {
  id: string;
  title: string;
  model: string | null;
  workspace_id: string | null;
  created_at: string;
  updated_at: string;
}

export class V2ChatClient {
  baseUrl: string;
  getToken: () => Promise<string>;

  constructor(baseUrl: string, getToken: () => Promise<string>) {
    this.baseUrl = baseUrl;
    this.getToken = getToken;
  }

  async createSession(opts: { model?: string; title?: string; workspace_id?: string }): Promise<V2ChatSession> {
    const token = await this.getToken();
    const res = await fetch(`${this.baseUrl}/api/v2/chat/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(opts),
    });
    if (!res.ok) throw new Error(`createSession: HTTP ${res.status}`);
    return res.json();
  }

  async listSessions(): Promise<{ sessions: V2ChatSession[] }> {
    const token = await this.getToken();
    const res = await fetch(`${this.baseUrl}/api/v2/chat/sessions`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) throw new Error(`listSessions: HTTP ${res.status}`);
    return res.json();
  }

  async send(chatSessionId: string, message: string, opts: {
    model?: string;
    effort?: string;
    web_search?: boolean;
    deep_research?: boolean;
    workspace_id?: string;
  }): Promise<{ session_id: string; chat_session_id: string; status: string }> {
    const token = await this.getToken();
    const res = await fetch(`${this.baseUrl}/api/v2/agent`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ message, chat_session_id: chatSessionId, ...opts }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    return res.json();
  }

  stream(
    sessionId: string,
    onEvent: (ev: V2AgentEvent) => void,
    onError: (err: Error) => void,
    onClose: () => void,
  ): AbortController {
    const ac = new AbortController();
    let retryCount = 0;
    const maxRetries = 3;

    const openStream = async () => {
      try {
        const token = await this.getToken();
        const url = `${this.baseUrl}/api/v2/agent/${sessionId}/stream?since=0`;
        const res = await fetch(url, {
          signal: ac.signal,
          headers: { Authorization: `Bearer ${token}` },
        });

        if (!res.ok) {
          if (retryCount < maxRetries && res.status !== 401 && !ac.signal.aborted) {
            retryCount++;
            await new Promise(r => setTimeout(r, 500));
            if (!ac.signal.aborted) openStream();
            return;
          }
          onError(new Error(`SSE: HTTP ${res.status}`));
          return;
        }

        const reader = res.body!.getReader();
        const decoder = new TextDecoder();
        let buf = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split("\n");
          buf = lines.pop() || "";
          for (const line of lines) {
            if (line.startsWith("data: ")) {
              try {
                const ev = JSON.parse(line.slice(6)) as V2AgentEvent;
                onEvent(ev);
              } catch { /* skip malformed */ }
            }
          }
        }
        onClose();
      } catch (e) {
        if ((e as Error)?.name === "AbortError") return;
        if (retryCount < maxRetries && !ac.signal.aborted) {
          retryCount++;
          await new Promise(r => setTimeout(r, 500));
          if (!ac.signal.aborted) openStream();
          return;
        }
        onError(e instanceof Error ? e : new Error(String(e)));
      }
    };

    openStream();
    return ac;
  }

  async poll(sessionId: string, since: number = 0): Promise<{ events: V2AgentEvent[]; status: string; next: number }> {
    const token = await this.getToken();
    const res = await fetch(`${this.baseUrl}/api/v2/agent/${sessionId}?since=${since}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) throw new Error(`poll: HTTP ${res.status}`);
    return res.json();
  }
}
