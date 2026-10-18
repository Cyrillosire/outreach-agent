export type SearchParams = Record<string, string | string[] | undefined>;

/** Build a query string from page search params, keeping only allowed, non-empty keys. */
export function buildQuery(params: SearchParams, allowed: readonly string[], overrides: SearchParams = {}): string {
  const merged: SearchParams = { ...params, ...overrides };
  const out = new URLSearchParams();
  for (const key of allowed) {
    const value = merged[key];
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item !== undefined && item !== "") out.append(key, item);
    }
  }
  const text = out.toString();
  return text ? `?${text}` : "";
}

export function firstParam(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

/** Positive integer page number from a search param (defaults to 1). */
export function pageParam(value: string | string[] | undefined): number {
  const parsed = Number.parseInt(firstParam(value) ?? "1", 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 1;
}
