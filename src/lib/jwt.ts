/** Extract the payload from a JWT string (no signature verification).
 *  Safe to use client-side — the backend still validates the signature. */
export function getJWTPayload(jwt: string): Record<string, unknown> | null {
  const parts = jwt.split(".");
  if (parts.length !== 3) return null;
  try {
    const base64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const json = atob(base64);
    return JSON.parse(json);
  } catch {
    return null;
  }
}

/** Get the `sub` claim from a JWT (the user_id). */
export function getJWTSub(jwt: string): string | null {
  const payload = getJWTPayload(jwt);
  return (payload?.sub as string) || null;
}
