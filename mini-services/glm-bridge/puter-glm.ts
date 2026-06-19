/**
 * Puter.js GLM client — FREE GLM 5.2 in the browser, zero setup.
 *
 * This is the "exactly like this chat" experience: the user visits the site,
 * Puter.js loads from CDN, they sign into their free Puter account (popup),
 * and get unlimited GLM 5.2. No API keys, no backend, no payment.
 *
 * Puter's "User-Pays" model: the developer pays $0. Each user authenticates
 * with their free Puter account and gets AI credits. If they exceed the free
 * quota, they pay Puter directly (not the developer).
 *
 * Usage:
 *   1. Add <script src="https://js.puter.com/v2/"></script> to index.html
 *   2. import { puterChat } from "./api/puter-glm"
 *   3. const response = await puterChat("Hello", [{role:"user",content:"Hi"}])
 *
 * The first call triggers a Puter sign-in popup if the user isn't authenticated.
 * After that, calls are seamless.
 */

// Puter.js is loaded from CDN — declare the global type
declare global {
  interface Window {
    puter?: {
      ai: {
        chat: (prompt: string | Array<{role: string; content: string}>, options?: {
          model?: string;
          stream?: boolean;
        }) => Promise<unknown>;
      };
      auth?: {
        signIn: () => Promise<void>;
        signOut: () => void;
        isSignedIn: () => boolean;
        getUser: () => Promise<{ username: string }>;
      };
    };
  }
}

export interface PuterChatOptions {
  model?: string;        // default "z-ai/glm-5.2"
  messages?: Array<{ role: string; content: string }>;
  onAuthRequired?: () => void;  // called when user needs to sign in
}

/**
 * Check if Puter.js is loaded and available.
 */
export function isPuterAvailable(): boolean {
  return typeof window !== "undefined" && !!window.puter?.ai?.chat;
}

/**
 * Check if the user is signed into Puter.
 */
export function isPuterSignedIn(): boolean {
  return isPuterAvailable() && (window.puter!.auth?.isSignedIn?.() ?? false);
}

/**
 * Sign in to Puter (triggers popup).
 */
export async function puterSignIn(): Promise<void> {
  if (!isPuterAvailable()) {
    throw new Error("Puter.js not loaded — check that https://js.puter.com/v2/ is in index.html");
  }
  await window.puter!.auth!.signIn();
}

/**
 * Get the Puter username (if signed in).
 */
export async function puterGetUser(): Promise<string | null> {
  if (!isPuterSignedIn()) return null;
  try {
    const user = await window.puter!.auth!.getUser();
    return user?.username || null;
  } catch {
    return null;
  }
}

/**
 * Call GLM 5.2 via Puter.js — FREE, no API key, no backend.
 *
 * The first call may trigger a sign-in popup. After that, calls are seamless.
 * Returns the response text.
 */
export async function puterChat(
  prompt: string,
  options?: PuterChatOptions,
): Promise<string> {
  if (!isPuterAvailable()) {
    throw new Error(
      "Puter.js not loaded. Add <script src=\"https://js.puter.com/v2/\"></script> to index.html.",
    );
  }

  const model = options?.model || "z-ai/glm-5.2";

  // Build the messages array. Puter.ai.chat accepts either a string or
  // a messages array. We pass the full conversation for multi-turn context.
  const messages = options?.messages || [{ role: "user", content: prompt }];

  try {
    const resp = await window.puter!.ai.chat(messages, { model });
    // Puter returns various shapes depending on the model — normalize
    if (typeof resp === "string") return resp;
    const r = resp as Record<string, unknown>;
    const content =
      (r.message as { content?: string })?.content ||
      (r as { content?: string }).content ||
      (r as { text?: string }).text ||
      String(resp);
    return content;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.toLowerCase().includes("auth") || msg.toLowerCase().includes("unauthorized")) {
      options?.onAuthRequired?.();
      throw new Error("Puter sign-in required. Click the sign-in button to continue.");
    }
    throw new Error(`Puter GLM error: ${msg}`);
  }
}

/**
 * Multi-turn chat with GLM 5.2 via Puter.js.
 * Maintains conversation history in memory (like the ZaiAdapter does).
 */
export class PuterGLMSession {
  private messages: Array<{ role: string; content: string }> = [];
  private model: string;

  constructor(model = "z-ai/glm-5.2", systemPrompt?: string) {
    this.model = model;
    if (systemPrompt) {
      this.messages.push({ role: "system", content: systemPrompt });
    }
  }

  async send(userMessage: string): Promise<string> {
    this.messages.push({ role: "user", content: userMessage });
    const response = await puterChat(userMessage, {
      model: this.model,
      messages: this.messages,
    });
    this.messages.push({ role: "assistant", content: response });
    return response;
  }

  clear() {
    this.messages = [];
  }

  get history() {
    return [...this.messages];
  }
}
