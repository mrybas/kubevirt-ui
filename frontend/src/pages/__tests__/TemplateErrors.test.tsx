/**
 * An error the user can act on.
 *
 * Creating a template named `ubuntu-small` in project `acme-dev` failed with
 * "Failed to create template. Please try again." The server had said 409 —
 * a template of that name already existed, left over pointing at the
 * long-deleted namespace `e2e-lab-prod`, and filtered out of the very list
 * the user was looking at. Retrying, as instructed, could never work.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';

const TEMPLATES = readFileSync(join(__dirname, '..', 'VMTemplates.tsx'), 'utf8');
const WIZARD = readFileSync(
  join(__dirname, '..', '..', 'components', 'vm', 'CreateVMWizard.tsx'), 'utf8',
);

describe('failures name their cause', () => {
  it('the template dialog shows the server message', () => {
    expect(TEMPLATES).not.toMatch(/Failed to \{isEditMode \? 'update' : 'create'\} template\. Please try again/);
    expect(TEMPLATES).toMatch(/error instanceof Error \? error\.message/);
  });

  it('the VM wizard shows the server message', () => {
    expect(WIZARD).not.toMatch(/Failed to create VM\. Please try again/);
    expect(WIZARD).toMatch(/createVM\.error instanceof Error/);
  });

  it('neither tells the user to retry a conflict', () => {
    for (const src of [TEMPLATES, WIZARD]) {
      expect(src).not.toMatch(/Please try again\./);
    }
  });
});
