/**
 * Profile API
 */

import { apiRequest } from './client';
import type { ProfileResponse, UpdateSSHKeysRequest } from '../types/profile';
export type { ProfileResponse, UpdateSSHKeysRequest };

export async function getProfile(): Promise<ProfileResponse> {
  return apiRequest<ProfileResponse>('/profile');
}

export async function updateSSHKeys(request: UpdateSSHKeysRequest): Promise<ProfileResponse> {
  return apiRequest<ProfileResponse>('/profile/ssh-keys', {
    method: 'PUT',
    body: request,
  });
}
