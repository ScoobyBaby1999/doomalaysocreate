// Derives the auto-rotating wire token from the root rotation secret, mirroring the
// backend's authtoken.py exactly: base64url(hmac_sha256(secret, str(floor(now/window)))[:18]).
// The secret stays on-device; only the short-lived derived token travels, and a leaked
// one self-expires when the window rolls. The server also accepts the previous window's
// token, so pass windowsBack=1 to retry across a boundary / small clock skew.

const WINDOW_S = 21600; // must match the server's TOKEN_WINDOW_S (6 hours)

export async function deriveToken(secret: string, windowsBack = 0): Promise<string> {
  if (!crypto?.subtle) {
    // WebCrypto needs a secure context; hf.space is always https, dev is localhost.
    throw new Error("rotating tokens need a secure (https) context");
  }
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const win = Math.floor(Date.now() / 1000 / WINDOW_S) - windowsBack;
  const mac = new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(String(win))));
  let bin = "";
  for (const b of mac.slice(0, 18)) bin += String.fromCharCode(b);
  // 18 bytes -> exactly 24 url-safe base64 chars, no padding (matches urlsafe_b64encode)
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_");
}
