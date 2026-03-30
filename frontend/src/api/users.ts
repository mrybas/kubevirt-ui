import { apiRequest } from './client';
import type {
  UserListResponse,
  GroupListResponse,
  LLDAPUser,
  LLDAPGroup,
  CreateUserRequest,
  UpdateUserRequest,
  CreateGroupRequest,
  AddMemberRequest,
} from '../types/users';

// Users
export async function listUsers(): Promise<UserListResponse> {
  return apiRequest<UserListResponse>('/users');
}

export async function getUser(userId: string): Promise<LLDAPUser> {
  return apiRequest<LLDAPUser>(`/users/${encodeURIComponent(userId)}`);
}

export async function createUser(data: CreateUserRequest): Promise<LLDAPUser> {
  return apiRequest<LLDAPUser>('/users', { method: 'POST', body: data });
}

export async function updateUser(userId: string, data: UpdateUserRequest): Promise<LLDAPUser> {
  return apiRequest<LLDAPUser>(`/users/${encodeURIComponent(userId)}`, { method: 'PUT', body: data });
}

export async function resetPassword(userId: string, password: string): Promise<void> {
  await apiRequest(`/users/${encodeURIComponent(userId)}/password`, { method: 'POST', body: { password } });
}

export async function disableUser(userId: string): Promise<void> {
  await apiRequest(`/users/${encodeURIComponent(userId)}/disable`, { method: 'POST' });
}

export async function enableUser(userId: string): Promise<void> {
  await apiRequest(`/users/${encodeURIComponent(userId)}/enable`, { method: 'POST' });
}

export async function deleteUser(userId: string): Promise<void> {
  await apiRequest(`/users/${encodeURIComponent(userId)}`, { method: 'DELETE' });
}

// Groups
export async function listGroups(): Promise<GroupListResponse> {
  return apiRequest<GroupListResponse>('/groups');
}

export async function getGroup(groupId: number): Promise<LLDAPGroup> {
  return apiRequest<LLDAPGroup>(`/groups/${groupId}`);
}

export async function createGroup(data: CreateGroupRequest): Promise<LLDAPGroup> {
  return apiRequest<LLDAPGroup>('/groups', { method: 'POST', body: data });
}

export async function deleteGroup(groupId: number): Promise<void> {
  await apiRequest(`/groups/${groupId}`, { method: 'DELETE' });
}

export async function addMember(groupId: number, data: AddMemberRequest): Promise<void> {
  await apiRequest(`/groups/${groupId}/members`, { method: 'POST', body: data });
}

export async function removeMember(groupId: number, memberId: string): Promise<void> {
  await apiRequest(`/groups/${groupId}/members/${encodeURIComponent(memberId)}`, { method: 'DELETE' });
}
