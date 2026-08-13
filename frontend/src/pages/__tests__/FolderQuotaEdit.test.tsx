/**
 * A folder's quota must be reachable after the folder exists.
 *
 * The detail page renders a quota panel ("No quota configured"), and the edit
 * dialog offered only Display Name and Description — so a quota could be set
 * at creation and never again, and never at all for a folder created without
 * one. `PATCH /folders/{name}` has accepted a quota the whole time
 * (folders.update_folder, including validation against the parent's).
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';

const SRC = readFileSync(join(__dirname, '..', 'FolderDetail.tsx'), 'utf8');

// The edit dialog only — the page has other quota references.
const EDIT = SRC.slice(SRC.indexOf('function EditFolderModal'), SRC.indexOf('function MoveFolderModal'));

describe('Edit Folder', () => {
  it('offers all three quota fields', () => {
    for (const label of ['Quota CPU', 'Quota memory', 'Quota storage']) {
      expect(EDIT).toContain(label);
    }
  });

  it('seeds them from the folder so editing does not silently clear a quota', () => {
    expect(EDIT).toMatch(/folder\.quota\?\.cpu/);
    expect(EDIT).toMatch(/folder\.quota\?\.memory/);
    expect(EDIT).toMatch(/folder\.quota\?\.storage/);
  });

  it('sends the quota in the update request', () => {
    expect(EDIT).toMatch(/\.\.\.\(quota \? \{ quota \} : \{\}\)/);
  });

  it('does not promise enforcement the cluster does not do', () => {
    // Nothing in the backend or the wizard refuses a VM over a folder quota;
    // it is a budget, and the dialog says so.
    expect(EDIT).toMatch(/not a limit the\s+cluster enforces/);
  });
});
