/**
 * The underlay form must not answer the Cilium questions for the cluster.
 *
 * Built with nothing ticked, the DaemonSet landed in `kube-system` while the
 * only `cilium-config` on the cluster — and the agent itself — live in
 * `o0-cilium`: the page substituted 'kube-system' for the empty field on its
 * way out.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';

const SRC = readFileSync(join(__dirname, '..', 'Underlay.tsx'), 'utf8');

describe('cilium fields default to unset', () => {
  it('the form starts with no opinion', () => {
    expect(SRC).toMatch(/cilium_source_ip_exempt:\s*null/);
    expect(SRC).toMatch(/cilium_namespace:\s*''/);
  });

  it('and sends the empty namespace through rather than kube-system', () => {
    expect(SRC).not.toMatch(/cilium_namespace: form\.cilium_namespace\.trim\(\) \|\| 'kube-system'/);
    expect(SRC).toMatch(/cilium_namespace: form\.cilium_namespace\.trim\(\),/);
  });

  it('shows the third state instead of an unticked box', () => {
    expect(SRC).toMatch(/indeterminate = form\.cilium_source_ip_exempt === null/);
  });
});
