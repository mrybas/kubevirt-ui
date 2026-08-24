/**
 * A control that destroys something says what it destroys.
 *
 * UAT run 4: on the VPC page the button that deletes the network was a bare
 * trash icon with no name and no text, two rows from the bare trash icon that
 * removes a peering. The tester reached for the peering and pressed the other
 * one. Nothing was lost — a confirmation dialog happened to be behind it —
 * but the network in question had two tenant clusters inside it.
 *
 * The product already owns `ConfirmDeleteModal`; it simply was not on every
 * destructive path. Removing a peering had no confirmation at all and cut
 * traffic between two VPCs on the click.
 *
 * This walks the source because that is where the property lives: any button
 * whose whole content is a delete icon must carry an accessible name. A test
 * that rendered one page would protect one page.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'fs';
import { join } from 'path';

const SRC = join(__dirname, '..', '..');

function tsxFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) return tsxFiles(full);
    return full.endsWith('.tsx') && !full.includes('__tests__') ? [full] : [];
  });
}

/** Buttons whose visible content is only a delete icon. */
function namelessDestructiveButtons(source: string): number[] {
  // Arrow functions are not tag ends.
  const src = source.replace(/=>/g, '=»');
  const lines: number[] = [];
  const pattern = /<button\b([\s\S]*?)>([\s\S]*?)<\/button>/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(src)) !== null) {
    const [, attrs, body] = match;
    if (!body.includes('Trash2')) continue;
    if (attrs.includes('title=') || attrs.includes('aria-label=')) continue;
    const withoutTags = body.replace(/<[^>]*>/g, '');
    // A quoted string, or plain words outside any expression, is a name.
    const hasLiteral = /['"][A-Za-z][^'"]*['"]/.test(withoutTags);
    const hasText = /[A-Za-z]{3,}/.test(withoutTags.replace(/\{[^{}]*\}/g, ''));
    if (hasLiteral || hasText) continue;
    lines.push(src.slice(0, match.index).split('\n').length);
  }
  return lines;
}

describe('destructive controls', () => {
  it('all carry a name', () => {
    const offenders: string[] = [];
    for (const file of tsxFiles(SRC)) {
      for (const line of namelessDestructiveButtons(readFileSync(file, 'utf8'))) {
        offenders.push(`${file.replace(SRC, 'src')}:${line}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it('the detector is not vacuous', () => {
    const nameless = `
      <button onClick={() =&gt; wipe()} className="p-2">
        <Trash2 className="h-4 w-4" />
      </button>`.replace('=&gt;', '=>');
    expect(namelessDestructiveButtons(nameless)).toHaveLength(1);

    const named = `
      <button onClick={() => wipe()} title="Delete the VPC">
        <Trash2 className="h-4 w-4" />
      </button>`;
    expect(namelessDestructiveButtons(named)).toHaveLength(0);

    const labelled = `
      <button onClick={() => wipe()}>
        <Trash2 className="h-4 w-4" />
        Delete VPC
      </button>`;
    expect(namelessDestructiveButtons(labelled)).toHaveLength(0);
  });
});

describe('removing a peering', () => {
  it('asks first, because it cuts live traffic between two VPCs', () => {
    const page = readFileSync(join(SRC, 'pages', 'VPCDetail.tsx'), 'utf8');
    const tab = page.slice(page.indexOf('function PeeringsTab'), page.indexOf('function RoutesTab'));
    // The click opens the confirmation; the mutation is behind it.
    expect(tab).toMatch(/onClick=\{\(\) => setPeeringToRemove\(p\.remote_vpc\)\}/);
    expect(tab).toContain('<ConfirmDeleteModal');
    expect(tab).toMatch(/resourceType="Peering"/);
  });

  it('names which peering it is', () => {
    const page = readFileSync(join(SRC, 'pages', 'VPCDetail.tsx'), 'utf8');
    expect(page).toMatch(/aria-label=\{`Remove the peering with \$\{p\.remote_vpc\}`\}/);
  });
});
