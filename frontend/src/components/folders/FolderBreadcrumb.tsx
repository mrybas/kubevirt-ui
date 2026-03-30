import { NavLink } from 'react-router-dom';
import { ChevronRight, Folder } from 'lucide-react';
import type { Folder as FolderType } from '../../types/folder';

interface FolderBreadcrumbProps {
  folder: FolderType;
  allFolders?: FolderType[];
  className?: string;
}

export function FolderBreadcrumb({ folder, allFolders = [], className }: FolderBreadcrumbProps) {
  // Build path: [root, ..., parent] + current
  const pathFolders = folder.path
    .map((name) => allFolders.find((f) => f.name === name))
    .filter(Boolean) as FolderType[];

  const items = [...pathFolders, folder];

  return (
    <nav className={`flex items-center gap-1 text-sm ${className ?? ''}`} aria-label="Folder breadcrumb">
      <NavLink
        to="/folders"
        className="text-surface-500 hover:text-surface-300 transition-colors"
        title="All Folders"
      >
        <Folder className="h-3.5 w-3.5" />
      </NavLink>

      {items.map((item, idx) => (
        <span key={item.name} className="flex items-center gap-1">
          <ChevronRight className="h-3.5 w-3.5 text-surface-600" />
          {idx === items.length - 1 ? (
            <span className="text-surface-200 font-medium">{item.display_name}</span>
          ) : (
            <NavLink
              to={`/folders/${item.name}`}
              className="text-surface-400 hover:text-surface-200 transition-colors"
            >
              {item.display_name}
            </NavLink>
          )}
        </span>
      ))}
    </nav>
  );
}
