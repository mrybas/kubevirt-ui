/**
 * Pure filter predicate for the Storage page's disk list.
 *
 * Extracted from Storage.tsx so it can be pinned with direct tests rather
 * than exercised only through a full page render + CustomSelect interaction.
 *
 * Catalogue rows (`origin === 'catalog'`) are exempt from the folder and
 * environment filters. They arrive from the backend with `namespace=""` and
 * no `environment` (see `backend/app/api/v1/images_catalog.py`) because they
 * exist outside the folder tree until materialised — they are candidates for
 * import into ANY folder, so filtering them by a folder/environment they are
 * not in yet has no meaning. Without this exemption they silently vanish the
 * moment either filter is active, with no banner or empty-state explaining
 * why — the same "something vanishes quietly" failure this feature has been
 * fighting elsewhere.
 *
 * Search and project filters still apply normally to catalogue rows — those
 * are about identity (what is this?), not placement (where does it live?).
 */

export interface FilterableDisk {
  name: string;
  display_name?: string;
  namespace: string;
  project?: string;
  environment?: string;
  origin?: 'cluster' | 'catalog';
}

export interface StorageFilterCriteria {
  searchQuery: string;
  filterProject: string;
  filterEnv: string;
  filterFolder: string;
  /** Namespaces belonging to the selected folder (including sub-folders). */
  folderNamespaces: Set<string>;
}

export function matchesStorageFilters(
  disk: FilterableDisk,
  { searchQuery, filterProject, filterEnv, filterFolder, folderNamespaces }: StorageFilterCriteria
): boolean {
  const isCatalogRow = disk.origin === 'catalog';
  const query = searchQuery.toLowerCase();

  const matchesSearch =
    disk.name.toLowerCase().includes(query) ||
    (disk.display_name?.toLowerCase().includes(query) ?? false);
  const matchesProject = !filterProject || disk.project === filterProject;
  const matchesEnv = !filterEnv || isCatalogRow || disk.environment === filterEnv;
  const matchesFolder = !filterFolder || isCatalogRow || folderNamespaces.has(disk.namespace);

  return matchesSearch && matchesProject && matchesEnv && matchesFolder;
}
