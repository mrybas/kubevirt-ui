/**
 * Quantities, and why a slider needs a unit.
 *
 * `parseQuantity` returning 0 for garbage would read as "this donor holds
 * nothing", which silently hides a donor instead of showing a broken quota.
 */
import { describe, it, expect } from 'vitest';
import {
  parseQuantity, formatBytes, pickUnit, sliderStep, trimNumber, BYTE_UNITS,
} from '../quantity';

describe('parseQuantity', () => {
  it('reads the binary suffixes a ResourceQuota uses', () => {
    expect(parseQuantity('16Gi')).toBe(16 * 2 ** 30);
    expect(parseQuantity('512Mi')).toBe(512 * 2 ** 20);
    expect(parseQuantity('1Ti')).toBe(2 ** 40);
  });

  it('reads millicores', () => {
    expect(parseQuantity('500m')).toBeCloseTo(0.5);
    expect(parseQuantity('8')).toBe(8);
  });

  it('returns null rather than 0 for what it cannot read', () => {
    for (const bad of ['', 'lots', '16GB!', null, undefined]) {
      expect(parseQuantity(bad as any)).toBeNull();
    }
  });
});

describe('formatBytes', () => {
  it('uses the unit it is given', () => {
    expect(formatBytes(4 * 2 ** 30, 'Gi')).toBe('4Gi');
    expect(formatBytes(4 * 2 ** 30, 'Mi')).toBe('4096Mi');
  });

  it('picks Gi only once there is a whole one', () => {
    expect(pickUnit(2 ** 30)).toBe('Gi');
    expect(pickUnit(2 ** 30 - 1)).toBe('Mi');
    expect(formatBytes(768 * 2 ** 20)).toBe('768Mi');
  });

  it('has an em dash for nothing', () => {
    expect(formatBytes(null)).toBe('—');
  });
});

describe('sliderStep', () => {
  it('is one unit for a range that is already fine-grained enough', () => {
    expect(sliderStep(64 * 2 ** 30, 'Gi')).toBe(BYTE_UNITS.Gi);
  });

  it('shrinks so a small donor can still give a part of itself', () => {
    // 2Gi in 1Gi notches is three positions and no way to hand over half.
    const step = sliderStep(2 * 2 ** 30, 'Gi');
    expect(2 * 2 ** 30 / step).toBeGreaterThanOrEqual(16);
    expect(step).toBeGreaterThanOrEqual(BYTE_UNITS.Mi);
  });

  it('grows so a huge donor does not get thousands of notches', () => {
    const step = sliderStep(4096 * 2 ** 20, 'Mi');
    expect(4096 * 2 ** 20 / step).toBeLessThanOrEqual(512);
  });
});

describe('trimNumber', () => {
  it('keeps integers whole and cuts trailing zeros', () => {
    expect(trimNumber(8)).toBe('8');
    expect(trimNumber(0.5)).toBe('0.5');
    expect(trimNumber(1.25)).toBe('1.25');
  });
});
