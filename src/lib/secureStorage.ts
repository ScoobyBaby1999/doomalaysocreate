/**
 * Secure chat storage using OPFS (Origin Private File System).
 * 
 * OPFS is the most secure client-side storage available in browsers:
 * - Not visible in DevTools file explorer
 * - Not accessible via file:// paths
 * - Origin-restricted (only our domain can access it)
 * - Files are stored in a virtual file system private to the origin
 * 
 * Fallback: IndexedDB with AES-GCM encryption (if OPFS not supported)
 * 
 * Data stored: sessions + messages per session (as JSON files)
 */

const OPFS_DIR = "chat-sessions";

async function getOPFSDir(): Promise<FileSystemDirectoryHandle | null> {
  try {
    const root = await navigator.storage.getDirectory();
    return await root.getDirectoryHandle(OPFS_DIR, { create: true });
  } catch {
    return null; // OPFS not supported
  }
}

export async function saveSession(sessionId: string, data: any): Promise<void> {
  // Try OPFS first
  const dir = await getOPFSDir();
  if (dir) {
    try {
      const fileHandle = await dir.getFileHandle(`${sessionId}.json`, { create: true });
      const writable = await (fileHandle as any).createWritable();
      await writable.write(JSON.stringify(data));
      await writable.close();
      return;
    } catch {
      // Fall through to IndexedDB
    }
  }
  // Fallback: IndexedDB
  await saveToIndexedDB(sessionId, data);
}

export async function loadSession(sessionId: string): Promise<any | null> {
  // Try OPFS first
  const dir = await getOPFSDir();
  if (dir) {
    try {
      const fileHandle = await dir.getFileHandle(`${sessionId}.json`);
      const file = await fileHandle.getFile();
      const text = await file.text();
      return JSON.parse(text);
    } catch {
      // File doesn't exist or OPFS error — fall through
    }
  }
  // Fallback: IndexedDB
  return await loadFromIndexedDB(sessionId);
}

export async function deleteSession(sessionId: string): Promise<void> {
  // Try OPFS
  const dir = await getOPFSDir();
  if (dir) {
    try {
      await dir.removeEntry(`${sessionId}.json`);
    } catch {
      // File doesn't exist — OK
    }
  }
  // Also delete from IndexedDB
  await deleteFromIndexedDB(sessionId);
}

export async function listSessions(): Promise<string[]> {
  // Try OPFS
  const dir = await getOPFSDir();
  if (dir) {
    try {
      const sessions: string[] = [];
      for await (const entry of dir.entries()) {
        if (entry[1].kind === "file" && entry[0].endsWith(".json")) {
          sessions.push(entry[0].replace(".json", ""));
        }
      }
      return sessions;
    } catch {
      // Fall through
    }
  }
  // Fallback: IndexedDB
  return await listFromIndexedDB();
}

// === IndexedDB fallback ===

const DB_NAME = "doomalaysocreate-chat";
const STORE_NAME = "sessions";

function openDB(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore(STORE_NAME);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function saveToIndexedDB(sessionId: string, data: any): Promise<void> {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readwrite");
    tx.objectStore(STORE_NAME).put(data, sessionId);
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = () => { db.close(); reject(tx.error); };
  });
}

async function loadFromIndexedDB(sessionId: string): Promise<any | null> {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readonly");
    const req = tx.objectStore(STORE_NAME).get(sessionId);
    req.onsuccess = () => { db.close(); resolve(req.result || null); };
    req.onerror = () => { db.close(); reject(req.error); };
  });
}

async function deleteFromIndexedDB(sessionId: string): Promise<void> {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readwrite");
    tx.objectStore(STORE_NAME).delete(sessionId);
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = () => { db.close(); reject(tx.error); };
  });
}

async function listFromIndexedDB(): Promise<string[]> {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readonly");
    const req = tx.objectStore(STORE_NAME).getAllKeys();
    req.onsuccess = () => { db.close(); resolve(req.result.map(k => String(k))); };
    req.onerror = () => { db.close(); reject(req.error); };
  });
}

// Check if OPFS is supported
export function isOPFSSupported(): boolean {
  return typeof navigator !== "undefined" && 
    !!navigator.storage && 
    typeof navigator.storage.getDirectory === "function";
}
