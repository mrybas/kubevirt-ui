/**
 * BGP API client
 * Matches backend app/api/v1/bgp.py endpoints (prefix: /bgp)
 */

import { apiRequest } from './client';
import type {
  SpeakerDeployRequest,
  SpeakerStatusResponse,
  AnnouncementRequest,
  AnnouncementResponse,
  BGPSessionResponse,
  GatewayConfigExample,
} from '../types/bgp';

const BASE = '/bgp';

export async function getSpeakerStatus(): Promise<SpeakerStatusResponse> {
  return apiRequest<SpeakerStatusResponse>(`${BASE}/speaker`);
}

export async function deploySpeaker(request: SpeakerDeployRequest): Promise<SpeakerStatusResponse> {
  return apiRequest<SpeakerStatusResponse>(`${BASE}/speaker`, { method: 'POST', body: request });
}

export async function updateSpeaker(request: SpeakerDeployRequest): Promise<SpeakerStatusResponse> {
  return apiRequest<SpeakerStatusResponse>(`${BASE}/speaker`, { method: 'PUT', body: request });
}

export async function deleteSpeaker(): Promise<void> {
  await apiRequest<void>(`${BASE}/speaker`, { method: 'DELETE' });
}

export async function listAnnouncements(): Promise<AnnouncementResponse[]> {
  return apiRequest<AnnouncementResponse[]>(`${BASE}/announcements`);
}

export async function createAnnouncement(request: AnnouncementRequest): Promise<AnnouncementResponse> {
  return apiRequest<AnnouncementResponse>(`${BASE}/announcements`, { method: 'POST', body: request });
}

export async function deleteAnnouncement(request: AnnouncementRequest): Promise<AnnouncementResponse> {
  return apiRequest<AnnouncementResponse>(`${BASE}/announcements`, { method: 'DELETE', body: request });
}

export async function listBgpSessions(): Promise<BGPSessionResponse[]> {
  return apiRequest<BGPSessionResponse[]>(`${BASE}/sessions`);
}

export type { GatewayConfigExample };

export async function getGatewayConfigExamples(): Promise<GatewayConfigExample[]> {
  return apiRequest<GatewayConfigExample[]>(`${BASE}/gateway-config`);
}
