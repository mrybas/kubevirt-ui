import { useState, useEffect } from 'react';
import { useUsers, useCreateUser, useUpdateUser, useDeleteUser, useResetPassword, useToggleUser, useGroups, useAddMember, useRemoveMember } from '../hooks/useUsers';
import { usePagination } from '../hooks/usePagination';
import type { LLDAPUser, CreateUserRequest, UpdateUserRequest } from '../types/users';
import {
  Users as UsersIcon,
  UserPlus,
  Pencil,
  Trash2,
  X,
  Shield,
  KeyRound,
  Ban,
  AlertTriangle,
} from 'lucide-react';
import { DataTable, type Column } from '@/components/common/DataTable';
import type { MenuItem } from '@/components/common/KebabMenu';
import { ActionBar } from '@/components/common/ActionBar';

export default function Users() {
  const { data, isLoading, error } = useUsers();
  const { data: groupsData } = useGroups();
  const createUser = useCreateUser();
  const updateUser = useUpdateUser();
  const deleteUser = useDeleteUser();
  const resetPwd = useResetPassword();
  const toggleUser = useToggleUser();
  const addMember = useAddMember();
  const removeMember = useRemoveMember();

  const [search, setSearch] = useState('');
  const [showCreateModal, setShowCreateModal] = useState(false);
  const { page, perPage, setPage, setPerPage } = usePagination(50);
  useEffect(() => { setPage(1); }, [search]); // eslint-disable-line react-hooks/exhaustive-deps
  const [editingUser, setEditingUser] = useState<LLDAPUser | null>(null);
  const [managingGroups, setManagingGroups] = useState<LLDAPUser | null>(null);
  const [resetPasswordUser, setResetPasswordUser] = useState<string | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);

  const users = data?.items ?? [];
  const groups = groupsData?.items ?? [];

  const filtered = users.filter(
    (u) =>
      u.id.toLowerCase().includes(search.toLowerCase()) ||
      u.email.toLowerCase().includes(search.toLowerCase()) ||
      u.display_name.toLowerCase().includes(search.toLowerCase())
  );
  const paginatedUsers = filtered.slice((page - 1) * perPage, page * perPage);

  if (error) {
    return (
      <div className="p-6">
        <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-6 text-center">
          <AlertTriangle className="mx-auto h-8 w-8 text-red-400 mb-3" />
          <h3 className="text-lg font-semibold text-red-400">Failed to Load Users</h3>
          <p className="mt-1 text-sm text-surface-400">{(error as Error).message}</p>
        </div>
      </div>
    );
  }

  const columns: Column<LLDAPUser>[] = [
    {
      key: 'username',
      header: 'Username',
      sortable: true,
      accessor: (user) => (
        <div className="flex items-center gap-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-full bg-surface-700 text-sm font-medium text-surface-300">
            {user.id[0]?.toUpperCase()}
          </div>
          <span className="font-medium text-surface-200">{user.id}</span>
          {user.groups.some((g) => g.display_name === 'kubevirt-ui-admins') && (
            <span title="Admin"><Shield className="h-3.5 w-3.5 text-amber-400" /></span>
          )}
        </div>
      ),
    },
    { key: 'email', header: 'Email', hideOnMobile: true, accessor: (user) => <span className="text-sm text-surface-400">{user.email}</span> },
    { key: 'display_name', header: 'Display Name', sortable: true, hideOnMobile: true, accessor: (user) => <span className="text-sm text-surface-300">{user.display_name}</span> },
    {
      key: 'status',
      header: 'Status',
      accessor: (user) => {
        const isDisabled = user.groups.some((g) => g.display_name === 'disabled-users');
        return (
          <button
            onClick={() => user.id !== 'admin' && toggleUser.mutate({ userId: user.id, disable: !isDisabled })}
            disabled={user.id === 'admin' || toggleUser.isPending}
            className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium transition-colors ${
              isDisabled ? 'bg-red-500/10 text-red-400 hover:bg-red-500/20' : 'bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20'
            } ${user.id === 'admin' ? 'cursor-default' : 'cursor-pointer'}`}
          >
            {isDisabled ? <Ban className="h-3 w-3" /> : null}
            {isDisabled ? 'Disabled' : 'Active'}
          </button>
        );
      },
    },
    {
      key: 'groups',
      header: 'Groups',
      hideOnMobile: true,
      accessor: (user) => (
        <div className="flex flex-wrap gap-1">
          {user.groups
            .filter((g) => g.display_name !== 'lldap_admin' && g.display_name !== 'disabled-users')
            .map((g) => (
              <span
                key={g.id}
                className="inline-flex items-center rounded-full bg-surface-700 px-2 py-0.5 text-xs text-surface-300 cursor-pointer hover:bg-surface-600"
                onClick={(e) => { e.stopPropagation(); setManagingGroups(user); }}
              >
                {g.display_name}
              </span>
            ))}
          {user.groups.filter((g) => g.display_name !== 'lldap_admin' && g.display_name !== 'disabled-users').length === 0 && (
            <span
              className="text-xs text-surface-500 cursor-pointer hover:text-surface-300"
              onClick={(e) => { e.stopPropagation(); setManagingGroups(user); }}
            >
              + Add group
            </span>
          )}
        </div>
      ),
    },
  ];

  const getActions = (user: LLDAPUser): MenuItem[] => {
    const items: MenuItem[] = [
      { label: 'Edit', icon: <Pencil className="h-4 w-4" />, onClick: () => setEditingUser(user) },
      { label: 'Reset Password', icon: <KeyRound className="h-4 w-4" />, onClick: () => setResetPasswordUser(user.id) },
    ];
    if (user.id !== 'admin') {
      items.push({ label: 'Delete', icon: <Trash2 className="h-4 w-4" />, onClick: () => setDeleteConfirm(user.id), variant: 'danger' });
    }
    return items;
  };

  return (
    <div className="space-y-6 p-6">
      <ActionBar
        title="Users"
        subtitle={`${users.length} user${users.length !== 1 ? 's' : ''} registered`}
      >
        <button onClick={() => setShowCreateModal(true)} className="btn-primary">
          <UserPlus className="h-4 w-4" />
          Create User
        </button>
      </ActionBar>

      <DataTable
        columns={columns}
        data={paginatedUsers}
        loading={isLoading}
        keyExtractor={(user) => user.id}
        actions={getActions}
        selectable
        bulkActions={[
          { label: 'Delete', icon: <Trash2 className="h-4 w-4" />, onClick: (items) => { (items as LLDAPUser[]).forEach((u) => { if (u.id !== 'admin') deleteUser.mutate(u.id); }); }, variant: 'danger' },
        ]}
        searchable
        searchPlaceholder="Search users..."
        onSearch={setSearch}
        pagination={{
          page,
          pageSize: perPage,
          total: filtered.length,
          onPageChange: setPage,
          onPageSizeChange: setPerPage,
        }}
        emptyState={{
          icon: <UsersIcon className="h-16 w-16" />,
          title: 'No users found',
          description: search ? 'No users match your search' : 'No users registered yet.',
        }}
      />

      {/* Create User Modal */}
      {showCreateModal && (
        <CreateUserModal
          onClose={() => setShowCreateModal(false)}
          onSubmit={(data) => {
            createUser.mutate(data, { onSuccess: () => setShowCreateModal(false) });
          }}
          isLoading={createUser.isPending}
        />
      )}

      {/* Edit User Modal */}
      {editingUser && (
        <EditUserModal
          user={editingUser}
          onClose={() => setEditingUser(null)}
          onSubmit={(data) => {
            updateUser.mutate(
              { userId: editingUser.id, data },
              { onSuccess: () => setEditingUser(null) }
            );
          }}
          isLoading={updateUser.isPending}
        />
      )}

      {/* Manage Groups Modal */}
      {managingGroups && (
        <ManageGroupsModal
          user={managingGroups}
          allGroups={groups}
          onClose={() => setManagingGroups(null)}
          onAddGroup={(groupId) => addMember.mutate({ groupId, userId: managingGroups.id })}
          onRemoveGroup={(groupId) => removeMember.mutate({ groupId, memberId: managingGroups.id })}
          isLoading={addMember.isPending || removeMember.isPending}
        />
      )}

      {/* Reset Password Modal */}
      {resetPasswordUser && (
        <ResetPasswordModal
          userId={resetPasswordUser}
          onClose={() => setResetPasswordUser(null)}
          onSubmit={(password) => {
            resetPwd.mutate(
              { userId: resetPasswordUser, password },
              { onSuccess: () => setResetPasswordUser(null) }
            );
          }}
          isLoading={resetPwd.isPending}
        />
      )}

      {/* Delete Confirmation */}
      {deleteConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="w-full max-w-sm rounded-xl border border-surface-700 bg-surface-900 p-6 shadow-2xl">
            <h3 className="text-lg font-semibold text-surface-100">Delete User</h3>
            <p className="mt-2 text-sm text-surface-400">
              Are you sure you want to delete <strong className="text-surface-200">{deleteConfirm}</strong>? This action cannot be undone.
            </p>
            <div className="mt-6 flex justify-end gap-3">
              <button
                onClick={() => setDeleteConfirm(null)}
                className="btn-secondary"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  deleteUser.mutate(deleteConfirm, { onSuccess: () => setDeleteConfirm(null) });
                }}
                className="btn-danger"
                disabled={deleteUser.isPending}
              >
                {deleteUser.isPending ? 'Deleting...' : 'Delete'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Create User Modal
// ---------------------------------------------------------------------------

function CreateUserModal({
  onClose,
  onSubmit,
  isLoading,
}: {
  onClose: () => void;
  onSubmit: (data: CreateUserRequest) => void;
  isLoading: boolean;
}) {
  const [form, setForm] = useState<CreateUserRequest>({
    id: '',
    email: '',
    display_name: '',
    password: '',
  });
  const [confirmPassword, setConfirmPassword] = useState('');
  const passwordMismatch = confirmPassword.length > 0 && form.password !== confirmPassword;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-full max-w-lg rounded-xl border border-surface-700 bg-surface-900 shadow-2xl">
        <div className="flex items-center justify-between border-b border-surface-800 px-6 py-4">
          <h2 className="text-lg font-semibold text-surface-100">Create User</h2>
          <button onClick={onClose} className="rounded p-1 text-surface-400 hover:text-surface-200">
            <X className="h-5 w-5" />
          </button>
        </div>
        <form
          onSubmit={(e) => { e.preventDefault(); if (!passwordMismatch) onSubmit(form); }}
          className="space-y-4 p-6"
        >
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Username</label>
            <input
              type="text"
              className="input w-full"
              value={form.id}
              onChange={(e) => setForm({ ...form, id: e.target.value })}
              required
              autoFocus
              placeholder="john.doe"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Email</label>
            <input
              type="email"
              className="input w-full"
              value={form.email}
              onChange={(e) => setForm({ ...form, email: e.target.value })}
              required
              placeholder="john@example.com"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Display Name</label>
            <input
              type="text"
              className="input w-full"
              value={form.display_name}
              onChange={(e) => setForm({ ...form, display_name: e.target.value })}
              placeholder="John Doe"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Password</label>
            <input
              type="password"
              className="input w-full"
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
              required
              minLength={6}
              placeholder="Minimum 6 characters"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Confirm Password</label>
            <input
              type="password"
              className={`input w-full ${passwordMismatch ? 'border-red-500 focus:border-red-500' : ''}`}
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
              minLength={6}
              placeholder="Re-enter password"
            />
            {passwordMismatch && (
              <p className="mt-1 text-xs text-red-400">Passwords do not match</p>
            )}
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">
              Cancel
            </button>
            <button
              type="submit"
              className="btn-primary"
              disabled={isLoading || !form.id || !form.email || !form.password || passwordMismatch || !confirmPassword}
            >
              {isLoading ? 'Creating...' : 'Create User'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Edit User Modal
// ---------------------------------------------------------------------------

function EditUserModal({
  user,
  onClose,
  onSubmit,
  isLoading,
}: {
  user: LLDAPUser;
  onClose: () => void;
  onSubmit: (data: UpdateUserRequest) => void;
  isLoading: boolean;
}) {
  const [form, setForm] = useState<UpdateUserRequest>({
    email: user.email,
    display_name: user.display_name,
  });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-full max-w-lg rounded-xl border border-surface-700 bg-surface-900 shadow-2xl">
        <div className="flex items-center justify-between border-b border-surface-800 px-6 py-4">
          <h2 className="text-lg font-semibold text-surface-100">Edit User: {user.id}</h2>
          <button onClick={onClose} className="rounded p-1 text-surface-400 hover:text-surface-200">
            <X className="h-5 w-5" />
          </button>
        </div>
        <form
          onSubmit={(e) => { e.preventDefault(); onSubmit(form); }}
          className="space-y-4 p-6"
        >
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Email</label>
            <input
              type="email"
              className="input w-full"
              value={form.email || ''}
              onChange={(e) => setForm({ ...form, email: e.target.value })}
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Display Name</label>
            <input
              type="text"
              className="input w-full"
              value={form.display_name || ''}
              onChange={(e) => setForm({ ...form, display_name: e.target.value })}
            />
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button type="submit" className="btn-primary" disabled={isLoading}>
              {isLoading ? 'Saving...' : 'Save Changes'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Manage Groups Modal
// ---------------------------------------------------------------------------

function ManageGroupsModal({
  user,
  allGroups,
  onClose,
  onAddGroup,
  onRemoveGroup,
  isLoading,
}: {
  user: LLDAPUser;
  allGroups: { id: number; display_name: string }[];
  onClose: () => void;
  onAddGroup: (groupId: number) => void;
  onRemoveGroup: (groupId: number) => void;
  isLoading: boolean;
}) {
  const userGroupIds = new Set(user.groups.map((g) => g.id));
  const availableGroups = allGroups.filter(
    (g) => !userGroupIds.has(g.id) && g.display_name !== 'lldap_admin'
  );
  const memberGroups = allGroups.filter(
    (g) => userGroupIds.has(g.id) && g.display_name !== 'lldap_admin'
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-xl border border-surface-700 bg-surface-900 shadow-2xl">
        <div className="flex items-center justify-between border-b border-surface-800 px-6 py-4">
          <h2 className="text-lg font-semibold text-surface-100">Groups: {user.id}</h2>
          <button onClick={onClose} className="rounded p-1 text-surface-400 hover:text-surface-200">
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className="p-6 space-y-4">
          {/* Current groups */}
          <div>
            <h3 className="text-sm font-medium text-surface-400 mb-2">Member of</h3>
            {memberGroups.length === 0 ? (
              <p className="text-sm text-surface-500">No groups</p>
            ) : (
              <div className="space-y-1">
                {memberGroups.map((g) => (
                  <div
                    key={g.id}
                    className="flex items-center justify-between rounded-lg bg-surface-800 px-3 py-2"
                  >
                    <span className="text-sm text-surface-200">{g.display_name}</span>
                    <button
                      onClick={() => onRemoveGroup(g.id)}
                      className="rounded p-1 text-surface-500 hover:text-red-400"
                      disabled={isLoading}
                      title="Remove from group"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Available groups */}
          {availableGroups.length > 0 && (
            <div>
              <h3 className="text-sm font-medium text-surface-400 mb-2">Add to group</h3>
              <div className="space-y-1">
                {availableGroups.map((g) => (
                  <button
                    key={g.id}
                    onClick={() => onAddGroup(g.id)}
                    className="flex w-full items-center rounded-lg border border-dashed border-surface-700 px-3 py-2 text-sm text-surface-400 hover:border-primary-500/50 hover:text-primary-400 transition-colors"
                    disabled={isLoading}
                  >
                    + {g.display_name}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="flex justify-end pt-2">
            <button onClick={onClose} className="btn-secondary">Done</button>
          </div>
        </div>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Reset Password Modal
// ---------------------------------------------------------------------------

function ResetPasswordModal({
  userId,
  onClose,
  onSubmit,
  isLoading,
}: {
  userId: string;
  onClose: () => void;
  onSubmit: (password: string) => void;
  isLoading: boolean;
}) {
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const passwordMismatch = confirmPassword.length > 0 && password !== confirmPassword;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-xl border border-surface-700 bg-surface-900 shadow-2xl">
        <div className="flex items-center justify-between border-b border-surface-800 px-6 py-4">
          <h2 className="text-lg font-semibold text-surface-100">Reset Password: {userId}</h2>
          <button onClick={onClose} className="rounded p-1 text-surface-400 hover:text-surface-200">
            <X className="h-5 w-5" />
          </button>
        </div>
        <form
          onSubmit={(e) => { e.preventDefault(); if (!passwordMismatch && password) onSubmit(password); }}
          className="space-y-4 p-6"
        >
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">New Password</label>
            <input
              type="password"
              className="input w-full"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={6}
              autoFocus
              placeholder="Minimum 6 characters"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Confirm Password</label>
            <input
              type="password"
              className={`input w-full ${passwordMismatch ? 'border-red-500 focus:border-red-500' : ''}`}
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
              minLength={6}
              placeholder="Re-enter password"
            />
            {passwordMismatch && (
              <p className="mt-1 text-xs text-red-400">Passwords do not match</p>
            )}
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button
              type="submit"
              className="btn-primary"
              disabled={isLoading || !password || passwordMismatch || !confirmPassword}
            >
              {isLoading ? 'Resetting...' : 'Reset Password'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
