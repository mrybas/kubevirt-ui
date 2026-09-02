/**
 * A catalogue row has no folder and no environment of its own — it arrives
 * from the backend with namespace="" (backend/app/api/v1/images_catalog.py)
 * because it exists outside the folder tree until materialised. Filtering it
 * by a folder or environment it is not in yet used to make it vanish
 * silently the moment either filter was active: no banner, no empty-state
 * explanation, just gone. A user filtering to their folder would reasonably
 * conclude the Harbor catalogue was empty.
 *
 * Catalogue rows are exempt from the folder and environment filters. Search
 * and project filters still apply normally — those are about identity, not
 * placement.
 */
import { describe, it, expect } from 'vitest';
import { matchesStorageFilters, type FilterableDisk } from '../storageFilters';

const cluster = (overrides: Partial<FilterableDisk> = {}): FilterableDisk => ({
  name: 'ubuntu',
  namespace: 'env-a',
  environment: 'env-a',
  project: 'proj-a',
  origin: 'cluster',
  ...overrides,
});

const catalog = (overrides: Partial<FilterableDisk> = {}): FilterableDisk => ({
  name: 'rocky-9:1',
  namespace: '',
  origin: 'catalog',
  ...overrides,
});

const baseCriteria = {
  searchQuery: '',
  filterProject: '',
  filterEnv: '',
  filterFolder: '',
  folderNamespaces: new Set<string>(),
};

describe('placement filters (folder, environment) exempt catalogue rows', () => {
  it('keeps a catalogue row visible under a folder filter, and drops a non-matching cluster row', () => {
    const criteria = { ...baseCriteria, filterFolder: 'team-folder', folderNamespaces: new Set(['env-b']) };

    expect(matchesStorageFilters(catalog(), criteria)).toBe(true);
    expect(matchesStorageFilters(cluster({ namespace: 'env-a' }), criteria)).toBe(false);
    expect(matchesStorageFilters(cluster({ namespace: 'env-b' }), criteria)).toBe(true);
  });

  it('keeps a catalogue row visible under an environment filter, and drops a non-matching cluster row', () => {
    const criteria = { ...baseCriteria, filterEnv: 'env-b' };

    expect(matchesStorageFilters(catalog(), criteria)).toBe(true);
    expect(matchesStorageFilters(cluster({ environment: 'env-a' }), criteria)).toBe(false);
    expect(matchesStorageFilters(cluster({ environment: 'env-b' }), criteria)).toBe(true);
  });

  it('does not exempt a catalogue row from search — the exemption is scoped to placement only', () => {
    const criteria = { ...baseCriteria, searchQuery: 'ubuntu' };

    expect(matchesStorageFilters(catalog({ name: 'rocky-9:1' }), criteria)).toBe(false);
    expect(matchesStorageFilters(catalog({ name: 'ubuntu-cloud' }), criteria)).toBe(true);
  });

  it('does not exempt a catalogue row from the project filter', () => {
    const criteria = { ...baseCriteria, filterProject: 'proj-a' };

    // A catalogue row has no project of its own; the project filter still
    // applies to it like any other identity filter.
    expect(matchesStorageFilters(catalog(), criteria)).toBe(false);
    expect(matchesStorageFilters(catalog({ project: 'proj-a' }), criteria)).toBe(true);
  });
});
