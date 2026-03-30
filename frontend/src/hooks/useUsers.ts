import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  listUsers,
  getUser,
  createUser,
  updateUser,
  resetPassword,
  disableUser,
  enableUser,
  deleteUser,
  listGroups,
  getGroup,
  createGroup,
  deleteGroup,
  addMember,
  removeMember,
} from '../api/users';
import type { CreateUserRequest, UpdateUserRequest, CreateGroupRequest } from '../types/users';
import { notify } from '../store/notifications';

// Users
export function useUsers() {
  return useQuery({
    queryKey: ['users'],
    queryFn: listUsers,
  });
}

export function useUser(userId: string) {
  return useQuery({
    queryKey: ['users', userId],
    queryFn: () => getUser(userId),
    enabled: !!userId,
  });
}

export function useCreateUser() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: CreateUserRequest) => createUser(data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['users'] });
      notify.success('User Created', `User "${variables.id}" created successfully`);
    },
    onError: (error: Error) => {
      notify.error('Failed to Create User', error.message);
    },
  });
}

export function useUpdateUser() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ userId, data }: { userId: string; data: UpdateUserRequest }) => updateUser(userId, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['users'] });
      notify.success('User Updated', `User "${variables.userId}" updated`);
    },
    onError: (error: Error) => {
      notify.error('Failed to Update User', error.message);
    },
  });
}

export function useDeleteUser() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (userId: string) => deleteUser(userId),
    onSuccess: (_, userId) => {
      queryClient.invalidateQueries({ queryKey: ['users'] });
      notify.success('User Deleted', `User "${userId}" deleted`);
    },
    onError: (error: Error) => {
      notify.error('Failed to Delete User', error.message);
    },
  });
}

export function useToggleUser() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ userId, disable }: { userId: string; disable: boolean }) =>
      disable ? disableUser(userId) : enableUser(userId),
    onSuccess: (_, { userId, disable }) => {
      queryClient.invalidateQueries({ queryKey: ['users'] });
      notify.success(
        disable ? 'User Disabled' : 'User Enabled',
        `User "${userId}" has been ${disable ? 'disabled' : 'enabled'}`
      );
    },
    onError: (error: Error) => {
      notify.error('Failed to Update User Status', error.message);
    },
  });
}

export function useResetPassword() {
  return useMutation({
    mutationFn: ({ userId, password }: { userId: string; password: string }) => resetPassword(userId, password),
    onSuccess: () => {
      notify.success('Password Reset', 'Password has been reset successfully');
    },
    onError: (error: Error) => {
      notify.error('Failed to Reset Password', error.message);
    },
  });
}

// Groups
export function useGroups() {
  return useQuery({
    queryKey: ['groups'],
    queryFn: listGroups,
  });
}

export function useGroup(groupId: number) {
  return useQuery({
    queryKey: ['groups', groupId],
    queryFn: () => getGroup(groupId),
    enabled: !!groupId,
  });
}

export function useCreateGroup() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: CreateGroupRequest) => createGroup(data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['groups'] });
      notify.success('Group Created', `Group "${variables.name}" created`);
    },
    onError: (error: Error) => {
      notify.error('Failed to Create Group', error.message);
    },
  });
}

export function useDeleteGroup() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (groupId: number) => deleteGroup(groupId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['groups'] });
      notify.success('Group Deleted', 'Group deleted successfully');
    },
    onError: (error: Error) => {
      notify.error('Failed to Delete Group', error.message);
    },
  });
}

export function useAddMember() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ groupId, userId }: { groupId: number; userId: string }) => addMember(groupId, { user_id: userId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['groups'] });
      queryClient.invalidateQueries({ queryKey: ['users'] });
      notify.success('Member Added', 'User added to group');
    },
    onError: (error: Error) => {
      notify.error('Failed to Add Member', error.message);
    },
  });
}

export function useRemoveMember() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ groupId, memberId }: { groupId: number; memberId: string }) => removeMember(groupId, memberId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['groups'] });
      queryClient.invalidateQueries({ queryKey: ['users'] });
      notify.success('Member Removed', 'User removed from group');
    },
    onError: (error: Error) => {
      notify.error('Failed to Remove Member', error.message);
    },
  });
}
