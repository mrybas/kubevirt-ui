import { useState } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import { FolderOpen, Folder, ChevronDown, ChevronRight, Server } from 'lucide-react';
import clsx from 'clsx';
import type { Folder as FolderType } from '../../types/folder';

interface FolderTreeNodeProps {
  folder: FolderType;
  level?: number;
}

function FolderTreeNode({ folder, level = 0 }: FolderTreeNodeProps) {
  const location = useLocation();
  const hasChildren = folder.children && folder.children.length > 0;

  // Auto-expand if current path is this folder or a descendant
  const isInPath = location.pathname.startsWith(`/folders/${folder.name}`);
  const [expanded, setExpanded] = useState(isInPath || level < 1);

  return (
    <div>
      <div className="flex items-center gap-0.5" style={{ paddingLeft: `${level * 12}px` }}>
        <button
          onClick={() => hasChildren && setExpanded(!expanded)}
          className={clsx(
            'w-5 h-6 flex items-center justify-center text-surface-500 hover:text-surface-300 shrink-0 transition-colors',
            !hasChildren && 'invisible'
          )}
        >
          {expanded ? (
            <ChevronDown className="h-3 w-3" />
          ) : (
            <ChevronRight className="h-3 w-3" />
          )}
        </button>

        <NavLink
          to={`/folders/${folder.name}`}
          className={({ isActive }) =>
            clsx(
              'flex flex-1 items-center gap-2 px-2 py-1.5 rounded-md text-sm transition-colors min-w-0',
              isActive
                ? 'bg-primary-500/10 text-primary-400'
                : 'text-surface-400 hover:bg-surface-800 hover:text-surface-200'
            )
          }
        >
          {({ isActive }) => (
            <>
              {isActive ? (
                <FolderOpen className="h-3.5 w-3.5 shrink-0" />
              ) : (
                <Folder className="h-3.5 w-3.5 shrink-0" />
              )}
              <span className="truncate flex-1">{folder.display_name}</span>
              {folder.total_vms > 0 && (
                <span className="flex items-center gap-0.5 text-xs text-surface-500 shrink-0">
                  <Server className="h-3 w-3" />
                  {folder.total_vms}
                </span>
              )}
            </>
          )}
        </NavLink>
      </div>

      {expanded && hasChildren && (
        <div>
          {folder.children.map((child) => (
            <FolderTreeNode key={child.name} folder={child} level={level + 1} />
          ))}
        </div>
      )}
    </div>
  );
}

interface FolderTreeProps {
  folders: FolderType[];
  isLoading?: boolean;
}

export function FolderTree({ folders, isLoading }: FolderTreeProps) {
  if (isLoading) {
    return (
      <div className="space-y-1 px-1">
        {[...Array(3)].map((_, i) => (
          <div key={i} className="h-7 bg-surface-800 rounded animate-pulse" />
        ))}
      </div>
    );
  }

  if (folders.length === 0) {
    return (
      <p className="px-3 py-1 text-xs text-surface-600 italic">No folders yet</p>
    );
  }

  return (
    <div className="space-y-0.5">
      {folders.map((folder) => (
        <FolderTreeNode key={folder.name} folder={folder} />
      ))}
    </div>
  );
}
