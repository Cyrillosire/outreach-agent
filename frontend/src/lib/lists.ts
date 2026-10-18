/** Parse "one item per line" text into a clean list (blank lines and surrounding spaces dropped). */
export function linesToList(text: string): string[] {
  return text.split("\n").map((l) => l.trim()).filter(Boolean);
}
