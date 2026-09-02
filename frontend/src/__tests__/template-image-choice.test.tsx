import { describe, it, expect } from 'vitest';
import { imageOptions } from '../pages/imageOptions';

describe('the template image chooser', () => {
  it('offers images that exist and images the catalogue can supply', () => {
    const opts = imageOptions([
      { name: 'ubuntu', origin: 'cluster', display_name: 'Ubuntu', size: '20Gi' },
      { name: 'rocky-9:1', origin: 'catalog', catalog_ref: 'p/rocky-9:1' },
    ]);
    expect(opts.map((o) => o.value)).toContain('ubuntu');
    expect(opts.map((o) => o.value)).toContain('p/rocky-9:1');
  });

  it('says which options still need importing, so the wait is not a surprise', () => {
    const opts = imageOptions([
      { name: 'rocky-9:1', origin: 'catalog', catalog_ref: 'p/rocky-9:1' },
    ]);
    expect(opts[0].label).toMatch(/import/i);
  });
});
