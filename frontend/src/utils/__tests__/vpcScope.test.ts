/**
 * Creating a VM in `acme-dev`, the Network step listed every VPC on the
 * cluster: `acme-net` (right), `beta-net` (another folder) and
 * `acme-prod-net` (scoped to the `prod` environment). The tenant wizard,
 * filtering on the same labels, showed only `acme-net`.
 */
import { describe, it, expect } from 'vitest';
import { isVpcInScope } from '../vpcScope';

describe('isVpcInScope', () => {
  it('keeps a VPC scoped to the selected folder', () => {
    expect(isVpcInScope({ folder: 'acme' }, 'acme', 'dev')).toBe(true);
  });

  it('hides a VPC belonging to another folder', () => {
    expect(isVpcInScope({ folder: 'beta' }, 'acme', 'dev')).toBe(false);
  });

  it('hides a VPC scoped to another environment of the same folder', () => {
    expect(isVpcInScope({ folder: 'acme', environment: 'prod' }, 'acme', 'dev')).toBe(false);
  });

  it('keeps a VPC scoped to exactly this environment', () => {
    expect(isVpcInScope({ folder: 'acme', environment: 'dev' }, 'acme', 'dev')).toBe(true);
  });

  it('keeps a global VPC — no folder label', () => {
    expect(isVpcInScope({ folder: null }, 'acme', 'dev')).toBe(true);
    expect(isVpcInScope({}, 'acme', 'dev')).toBe(true);
  });

  it('keeps a VPC it knows nothing about rather than hiding it', () => {
    expect(isVpcInScope(undefined, 'acme', 'dev')).toBe(true);
  });

  it('hides a folder-scoped VPC when nothing is selected yet', () => {
    expect(isVpcInScope({ folder: 'acme' }, '', undefined)).toBe(false);
  });
});

describe('the VM wizard actually applies it', () => {
  it('filters the network step by VPC scope', () => {
    const { readFileSync } = require('fs');
    const { join } = require('path');
    const src = readFileSync(
      join(__dirname, '..', '..', 'components', 'vm', 'CreateVMWizard.tsx'), 'utf8',
    );
    expect(src).toContain('isVpcInScope');
    expect(src).toMatch(/inScope\(s\.vpc\)/);
  });
});
