/**
 * Kubernetes quantities, in units a person picked rather than in bytes.
 *
 * A slider over bytes is unusable: 16Gi is 17179869184, so the thumb moves in
 * steps nobody asked for and the label reads as noise. Everything here works
 * in a chosen unit — Mi or Gi — and only converts to bytes at the edges,
 * where the API wants a quantity string.
 */

export type ByteUnit = 'Mi' | 'Gi';

export const BYTE_UNITS: Record<ByteUnit, number> = {
  Mi: 2 ** 20,
  Gi: 2 ** 30,
};

const SUFFIXES: Record<string, number> = {
  '': 1,
  k: 1e3, M: 1e6, G: 1e9, T: 1e12,
  Ki: 1024, Mi: 1024 ** 2, Gi: 1024 ** 3, Ti: 1024 ** 4,
  m: 1e-3,
};

/**
 * "16Gi" → 17179869184, "500m" → 0.5, "8" → 8.
 *
 * Returns null for anything unparseable rather than 0 — a quota that failed to
 * parse must not read as "this donor has nothing to give".
 */
export function parseQuantity(value: string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const m = String(value).trim().match(/^(\d+(?:\.\d+)?)([a-zA-Z]*)$/);
  if (!m) return null;
  const mult = SUFFIXES[m[2]];
  if (mult === undefined) return null;
  return Number(m[1]) * mult;
}

/** Drops a trailing .00 / .50 → .5 so slider labels stay short. */
export function trimNumber(n: number): string {
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
}

/** Gi once there is a whole one of them, Mi below that. */
export function pickUnit(bytes: number): ByteUnit {
  return bytes >= BYTE_UNITS.Gi ? 'Gi' : 'Mi';
}

/** Bytes rendered in the given unit: 17179869184, 'Gi' → "16Gi". */
export function formatBytes(bytes: number | null, unit?: ByteUnit): string {
  if (bytes === null) return '—';
  const u = unit ?? pickUnit(bytes);
  return `${trimNumber(bytes / BYTE_UNITS[u])}${u}`;
}

/**
 * How much one slider notch is worth, in bytes.
 *
 * One whole unit normally, but a 2Gi donor sliced in 1Gi steps gives three
 * positions and no way to hand over half of it — so the step shrinks until
 * the range has at least 16 notches, and grows so it never has more than 512.
 */
export function sliderStep(maxBytes: number, unit: ByteUnit): number {
  let step = BYTE_UNITS[unit];
  while (step > BYTE_UNITS.Mi && maxBytes / step < 16) step /= 2;
  while (maxBytes / step > 512) step *= 2;
  return Math.max(1, Math.round(step));
}
