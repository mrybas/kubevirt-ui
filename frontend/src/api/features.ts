import { apiRequest } from './client';

export interface Features {
  enableTenants: boolean;
}

export function getFeatures(): Promise<Features> {
  return apiRequest<Features>('/features');
}
