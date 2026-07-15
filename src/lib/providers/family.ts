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

export function isFreeModel(id: string): boolean {
  return /:free$/i.test(id) || /-free$/i.test(id);
}
