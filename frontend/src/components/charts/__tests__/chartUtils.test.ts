import { describe, it, expect } from 'vitest';
import { fmtBytes, fmtBytesPerSec, fmtPercent, fmtIOPS, seriesLabel } from '../chartUtils';

describe('fmtBytes', () => {
  it('formats gigabytes', () => {
    expect(fmtBytes(2.5e9)).toBe('2.5 GB');
  });

  it('formats megabytes', () => {
    expect(fmtBytes(150e6)).toBe('150.0 MB');
  });

  it('formats kilobytes', () => {
    expect(fmtBytes(4096)).toBe('4.1 KB');
  });

  it('formats bytes', () => {
    expect(fmtBytes(512)).toBe('512 B');
  });
});

describe('fmtBytesPerSec', () => {
  it('formats GB/s', () => {
    expect(fmtBytesPerSec(1.2e9)).toBe('1.2 GB/s');
  });

  it('formats MB/s', () => {
    expect(fmtBytesPerSec(50e6)).toBe('50.0 MB/s');
  });

  it('formats KB/s', () => {
    expect(fmtBytesPerSec(8000)).toBe('8.0 KB/s');
  });

  it('formats B/s', () => {
    expect(fmtBytesPerSec(100)).toBe('100 B/s');
  });
});

describe('fmtPercent', () => {
  it('formats percentage with one decimal', () => {
    expect(fmtPercent(85.678)).toBe('85.7%');
  });
});

describe('fmtIOPS', () => {
  it('formats thousands as K', () => {
    expect(fmtIOPS(5400)).toBe('5.4K');
  });

  it('formats small values as-is', () => {
    expect(fmtIOPS(42)).toBe('42');
  });
});

describe('seriesLabel', () => {
  it('returns value of primary labelKey', () => {
    expect(seriesLabel({ name: 'cpu0', instance: 'node1' })).toBe('cpu0');
  });

  it('falls back to instance/pod/node', () => {
    expect(seriesLabel({ instance: 'node1', pod: 'p1' })).toBe('node1');
    expect(seriesLabel({ pod: 'p1' })).toBe('p1');
    expect(seriesLabel({ node: 'n1' })).toBe('n1');
  });

  it('returns JSON stringified metric when no known keys match', () => {
    const metric = { custom: 'val' };
    expect(seriesLabel(metric)).toBe(JSON.stringify(metric));
  });

  it('uses custom labelKey', () => {
    expect(seriesLabel({ device: 'eth0' }, 'device')).toBe('eth0');
  });
});
