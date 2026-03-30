import { useState } from 'react';
import { useGroups, useCreateGroup, useDeleteGroup, useAddMember, useRemoveMember, useUsers } from '../hooks/useUsers';
import {
  Shield,
  Plus,
  Trash2,
  X,
  AlertTriangle,
  UserPlus,
  UserMinus,
} from 'lucide-react';
import { DataTable, type Column } from '@/components/common/DataTable';
import type { MenuItem } from '@/components/common/KebabMenu';
import { ActionBar } from '@/components/common/ActionBar';

export default function Groups() {
  const { data, isLoading, error } = useGroups();
  const { data: usersData } = useUsers();
  const createGroup = useCreateGroup();
  const deleteGroup = useDeleteGroup();
  const addMember = useAddMember();
  const removeMember = useRemoveMember();

  const [search, setSearch] = useState('');
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [addingMemberTo, setAddingMemberTo] = useState<number | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<number | null>(null);

  const groups = (data?.items ?? []).filter(g => g.display_name !== 'lldap_admin');
  const allUsers = usersData?.items ?? [];

  const filtered = groups.filter((g) =>
    g.display_name.toLowerCase().includes(search.toLowerCase())
  );

  if (error) {
    return (
      <div className="p-6">
        <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-6 text-center">
          <AlertTriangle className="mx-auto h-8 w-8 text-red-400 mb-3" />
          <h3 className="text-lg font-semibold text-red-400">Failed to Load Groups</h3>
          <p className="mt-1 text-sm text-surface-400">{(error as Error).message}</p>
        </div>
      </div>
    );
  }

  type GroupItem = typeof groups[number];

  const columns: Column<GroupItem>[] = [
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (group) => (
        <div className="flex items-center gap-2">
          <span className="font-medium text-surface-200">{group.display_name}</span>
          {group.display_name === 'kubevirt-ui-admins' && (
            <span className="inline-flex items-center rounded-full bg-amber-500/10 px-2 py-0.5 text-xs text-amber-400">System</span>
          )}
        </div>
      ),
    },
    {
      key: 'members',
      header: 'Members',
      hideOnMobile: true,
      accessor: (group) => <span>{group.member_count}</span>,
    },
  ];

  const getActions = (group: GroupItem): MenuItem[] => {
    const items: MenuItem[] = [];
    if (group.display_name !== 'kubevirt-ui-admins') {
      items.push({ label: 'Delete', icon: <Trash2 className="h-4 w-4" />, onClick: () => setDeleteConfirm(group.id), variant: 'danger' });
    }
    return items;
  };

  const renderExpandedRow = (group: GroupItem) => {
    const memberIds = new Set(group.members.map(m => m.id));
    const availableUsers = allUsers.filter(u => !memberIds.has(u.id));

    return (
      <div>
        <div className="flex items-center justify-between mb-3">
          <h4 className="text-sm font-medium text-surface-400">Members</h4>
          <button
            onClick={() => setAddingMemberTo(addingMemberTo === group.id ? null : group.id)}
            className="flex items-center gap-1 text-xs text-primary-400 hover:text-primary-300"
          >
            <UserPlus className="h-3.5 w-3.5" />
            Add Member
          </button>
        </div>

        {addingMemberTo === group.id && availableUsers.length > 0 && (
          <div className="mb-3 rounded-lg border border-surface-700 bg-surface-800 p-2 max-h-40 overflow-y-auto">
            {availableUsers.map((u) => (
              <button
                key={u.id}
                onClick={() => {
                  addMember.mutate({ groupId: group.id, userId: u.id });
                  setAddingMemberTo(null);
                }}
                className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-sm text-surface-300 hover:bg-surface-700"
                disabled={addMember.isPending}
              >
                <div className="flex h-6 w-6 items-center justify-center rounded-full bg-surface-600 text-xs">
                  {u.id[0]?.toUpperCase()}
                </div>
                <span>{u.id}</span>
                <span className="text-surface-500">({u.email})</span>
              </button>
            ))}
          </div>
        )}

        {group.members.length === 0 ? (
          <p className="text-sm text-surface-500 py-2">No members</p>
        ) : (
          <div className="space-y-1">
            {group.members.map((member) => (
              <div key={member.id} className="flex items-center justify-between rounded-lg bg-surface-800/50 px-3 py-2">
                <div className="flex items-center gap-2">
                  <div className="flex h-7 w-7 items-center justify-center rounded-full bg-surface-700 text-xs font-medium text-surface-300">
                    {member.id[0]?.toUpperCase()}
                  </div>
                  <div>
                    <span className="text-sm text-surface-200">{member.id}</span>
                    <span className="ml-2 text-xs text-surface-500">{member.email}</span>
                  </div>
                </div>
                <button
                  onClick={() => removeMember.mutate({ groupId: group.id, memberId: member.id })}
                  className="rounded p-1 text-surface-500 hover:text-red-400"
                  disabled={removeMember.isPending}
                  title="Remove from group"
                >
                  <UserMinus className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-6 p-6">
      <ActionBar
        title="Groups"
        subtitle={`${groups.length} group${groups.length !== 1 ? 's' : ''}`}
      >
        <button onClick={() => setShowCreateModal(true)} className="btn-primary">
          <Plus className="h-4 w-4" />
          Create Group
        </button>
      </ActionBar>

      <DataTable
        columns={columns}
        data={filtered}
        loading={isLoading}
        keyExtractor={(group) => String(group.id)}
        actions={getActions}
        expandable={renderExpandedRow}
        searchable
        searchPlaceholder="Search groups..."
        onSearch={setSearch}
        emptyState={{
          icon: <Shield className="h-16 w-16" />,
          title: 'No groups found',
          description: search ? 'No groups match your search' : 'Create a group to organize users.',
          action: !search ? (
            <button onClick={() => setShowCreateModal(true)} className="btn-primary">
              <Plus className="h-4 w-4" />
              Create Group
            </button>
          ) : undefined,
        }}
      />

      {/* Create Group Modal */}
      {showCreateModal && (
        <CreateGroupModal
          onClose={() => setShowCreateModal(false)}
          onSubmit={(name) => {
            createGroup.mutate({ name }, { onSuccess: () => setShowCreateModal(false) });
          }}
          isLoading={createGroup.isPending}
        />
      )}

      {/* Delete Confirmation */}
      {deleteConfirm !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="w-full max-w-sm rounded-xl border border-surface-700 bg-surface-900 p-6 shadow-2xl">
            <h3 className="text-lg font-semibold text-surface-100">Delete Group</h3>
            <p className="mt-2 text-sm text-surface-400">
              Are you sure you want to delete this group? All members will be removed.
            </p>
            <div className="mt-6 flex justify-end gap-3">
              <button onClick={() => setDeleteConfirm(null)} className="btn-secondary">Cancel</button>
              <button
                onClick={() => {
                  deleteGroup.mutate(deleteConfirm, { onSuccess: () => setDeleteConfirm(null) });
                }}
                className="btn-danger"
                disabled={deleteGroup.isPending}
              >
                {deleteGroup.isPending ? 'Deleting...' : 'Delete'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}


function CreateGroupModal({
  onClose,
  onSubmit,
  isLoading,
}: {
  onClose: () => void;
  onSubmit: (name: string) => void;
  isLoading: boolean;
}) {
  const [name, setName] = useState('');

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-full max-w-sm rounded-xl border border-surface-700 bg-surface-900 shadow-2xl">
        <div className="flex items-center justify-between border-b border-surface-800 px-6 py-4">
          <h2 className="text-lg font-semibold text-surface-100">Create Group</h2>
          <button onClick={onClose} className="rounded p-1 text-surface-400 hover:text-surface-200">
            <X className="h-5 w-5" />
          </button>
        </div>
        <form
          onSubmit={(e) => { e.preventDefault(); onSubmit(name); }}
          className="space-y-4 p-6"
        >
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Group Name</label>
            <input
              type="text"
              className="input w-full"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              autoFocus
              placeholder="e.g. developers, qa-team"
            />
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button type="submit" className="btn-primary" disabled={isLoading || !name.trim()}>
              {isLoading ? 'Creating...' : 'Create Group'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
