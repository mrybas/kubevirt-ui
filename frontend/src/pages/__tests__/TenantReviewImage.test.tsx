/**
 * The wizard offers one kind of worker, because there is one kind.
 *
 * It used to offer two, and the review then had to be careful: `handleSubmit`
 * sent `worker_image_url` only for cloud-init, but the review printed it
 * regardless, so choosing Talos showed
 *
 *     Image   quay.io/capk/ubuntu-2404-container-disk:v1.32.1
 *
 * while the worker that came up booted the Talos golden DataVolume. The review
 * was describing a different cluster.
 *
 * The care is gone with the choice. The operator builds Talos workers only —
 * `reconcileWorkers` answers `CloudInitNotMigrated` and stops — so offering the
 * other one led to a tenant whose machines never join, with the reason arriving
 * later as a condition on an object nobody is watching.
 *
 * This asserts the removal rather than the gating. The backend refuses the
 * value too, and that test is the one with teeth: a choice taken off a screen
 * is not a choice taken out of an API.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';

const SRC = readFileSync(join(__dirname, '..', 'Tenants.tsx'), 'utf8');

describe('the tenant wizard', () => {
  it('does not offer cloud-init', () => {
    // Comments explaining the removal are fine; a value, a branch or a label
    // is not.
    const code = SRC.split('\n')
      .filter(line => !line.trim().startsWith('//') && !line.trim().startsWith('*'))
      .join('\n');
    expect(code).not.toMatch(/'cloud-init'/);
    expect(code).not.toMatch(/Standard \(cloud-init\)/);
  });

  it('sends no container disk', () => {
    // A Talos worker boots a raw image the backend imports; a container-disk
    // reference on that path is rejected by CDI after the tenant's secrets and
    // PKI are already written.
    expect(SRC).not.toMatch(/worker_image_url/);
    expect(SRC).not.toMatch(/worker_image_source_type/);
  });

  it('still says which Talos release the workers get', () => {
    expect(SRC).toMatch(/Talos \$\{form\.talos_version \|\| talosDefault\}/);
  });
});
