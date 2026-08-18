/**
 * The review must not name an image the build will not use.
 *
 * `handleSubmit` sends `worker_image_url` only for cloud-init workers, but the
 * review printed it regardless. Choosing Talos, the screen said
 *
 *     Image   quay.io/capk/ubuntu-2404-container-disk:v1.32.1
 *
 * while the worker VM that actually came up booted the Talos golden
 * DataVolume (`t-talos-talos-golden`, OS-IMAGE `Talos (v1.13.8)` on the
 * joined node). The review was describing a different cluster.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';

const SRC = readFileSync(join(__dirname, '..', 'Tenants.tsx'), 'utf8');

describe('tenant review, worker image', () => {
  it('gates the Image row on cloud-init', () => {
    // The row must be conditional on the OS, not on the field being set.
    expect(SRC).toMatch(/worker_os === 'cloud-init' && form\.worker_image_url/);
  });

  it('never renders the image on the unconditional field alone', () => {
    // The shipped form: `{form.worker_image_url && (` opening the Image row.
    const bare = /\{form\.worker_image_url && \(\s*<>\s*<span className="text-surface-500">Image<\/span>/;
    expect(SRC).not.toMatch(bare);
  });

  it('says which worker OS was chosen, so the reader can tell', () => {
    // Asserted as meaning, not as text. This held the exact ternary until T4
    // made the Talos branch print the pair — "Talos 1.13.8 / Kubernetes
    // v1.33.5" — and the test failed while the property it guards was not only
    // intact but stronger. A test that pins a string pins the wrong thing.
    expect(SRC).toContain('Worker OS');
    expect(SRC).toMatch(/worker_os === 'talos'\s*\n?\s*\?/);
    expect(SRC).toContain("'Standard (cloud-init)'");
  });

  it('names the Talos and Kubernetes versions together', () => {
    // The pair is what the operator is choosing; either number alone is not
    // a decision they can check.
    expect(SRC).toMatch(/Talos \$\{form\.talos_version \|\| talosDefault\}/);
    expect(SRC).toMatch(/Kubernetes \$\{form\.kubernetes_version\}/);
  });

  it('still only submits the image for cloud-init', () => {
    // Guards the invariant the review now mirrors.
    expect(SRC).toMatch(/form\.worker_image_url && form\.worker_os === 'cloud-init'/);
  });
});
