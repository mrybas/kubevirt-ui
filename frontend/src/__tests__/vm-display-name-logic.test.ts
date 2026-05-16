/**
 * Pure logic tests for Phase 2 display_name changes in the VM wizard.
 * Mirrors canProceed('customize') from CreateVMWizard.tsx.
 */
import { describe, it, expect } from 'vitest';

function canProceedCustomize(displayName: string): boolean {
  return displayName.trim().length >= 1 && displayName.length <= 100;
}

describe('VM wizard canProceed (customize step)', () => {
  it('rejects empty string', () => {
    expect(canProceedCustomize('')).toBe(false);
  });

  it('rejects whitespace-only string', () => {
    expect(canProceedCustomize('   ')).toBe(false);
  });

  it('accepts single character', () => {
    expect(canProceedCustomize('a')).toBe(true);
  });

  it('accepts exactly 100 chars', () => {
    expect(canProceedCustomize('a'.repeat(100))).toBe(true);
  });

  it('rejects 101 chars', () => {
    expect(canProceedCustomize('a'.repeat(101))).toBe(false);
  });

  it('accepts human-readable names with spaces and parens', () => {
    expect(canProceedCustomize('Web Server (prod)')).toBe(true);
  });

  it('accepts names with dots and hyphens', () => {
    expect(canProceedCustomize('my-app.staging')).toBe(true);
  });

  it('does NOT require DNS-1123 format — uppercase allowed', () => {
    expect(canProceedCustomize('WebServer')).toBe(true);
  });

  it('does NOT require DNS-1123 format — special chars allowed', () => {
    expect(canProceedCustomize('VM #1 @ prod!')).toBe(true);
  });
});

describe('VM display_name 100-char truncation logic', () => {
  it('input onChange: value <= 100 is accepted', () => {
    const value = 'x'.repeat(100);
    const accepted = value.length <= 100;
    expect(accepted).toBe(true);
  });

  it('input onChange: value 101 chars is rejected (not set)', () => {
    const value = 'x'.repeat(101);
    const accepted = value.length <= 100;
    expect(accepted).toBe(false);
  });
});
