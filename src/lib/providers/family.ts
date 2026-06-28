const SUFFIXES = [
  ":free",
  "-instruct-2507",
  "-instruct-fp8-fast",
  "-instruct-fast",
  "-fp8-fast",
  "-instruct",
  "-versatile",
  "-it",
  "-chat",
  "-preview",
  "-free",
];

const SUFFIXES_SORTED = [...SUFFIXES].sort((a, b) => b.length - a.length);

export function makeFamily(id: string): string {
  let m = id.toLowerCase().trim();
  const slash = m.lastIndexOf("/");
  if (slash >= 0) m = m.slice(slash + 1);
  if (m.startsWith("@")) m = m.slice(1);
  let changed = true;
  while (changed) {
    changed = false;
    for (const s of SUFFIXES_SORTED) {
      if (m.endsWith(s)) {
        m = m.slice(0, -s.length);
        changed = true;
        break;
      }
    }
  }
  return m;
}

const ACRONYMS = new Set(["glm", "gpt", "oss", "llm", "mimo", "api", "fp8"]);

export function deriveDisplayName(id: string): string {
  const fam = makeFamily(id);
  const tokens = fam.split("-").filter(Boolean);
  const pretty = tokens.map((t) => {
    if (/^\d+(\.\d+)*$/.test(t)) return t;
    if (ACRONYMS.has(t.toLowerCase())) return t.toUpperCase();
    return t.charAt(0).toUpperCase() + t.slice(1);
  });
  let name = pretty.join(" ");
  if (/:free$/i.test(id) || /-free$/i.test(id) || /\bfree\b/i.test(id)) {
    name += " (Free)";
  }
  return name;
}

export function deriveCapabilitiesFromName(id: string): string[] {
  const fam = makeFamily(id);
  const caps: string[] = [];
  if (/\b(gemma|kimi|llama-4|qwen3|glm-5|step)\b/.test(fam) || /vision/.test(fam)) {
    caps.push("vision");
  }
  if (/\bcoder\b/.test(fam)) {
    caps.push("coder");
  }
  return caps;
}

export function isFreeModel(id: string): boolean {
  return /:free$/i.test(id) || /-free$/i.test(id);
}
