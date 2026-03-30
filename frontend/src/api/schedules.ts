import { apiRequest } from './client';
import type { ScheduleInfo, CreateScheduleRequest } from '../types/schedule';
export type { ScheduleInfo, CreateScheduleRequest };

export async function listSchedules(
  namespace: string,
  vmName?: string
): Promise<ScheduleInfo[]> {
  const params = vmName ? `?vm_name=${encodeURIComponent(vmName)}` : '';
  return apiRequest<ScheduleInfo[]>(
    `/namespaces/${namespace}/schedules${params}`
  );
}

export async function createSchedule(
  namespace: string,
  data: CreateScheduleRequest
): Promise<ScheduleInfo> {
  return apiRequest<ScheduleInfo>(
    `/namespaces/${namespace}/schedules`,
    { method: 'POST', body: data }
  );
}

export async function deleteSchedule(
  namespace: string,
  name: string
): Promise<void> {
  await apiRequest<void>(
    `/namespaces/${namespace}/schedules/${name}`,
    { method: 'DELETE' }
  );
}

export async function updateSchedule(
  namespace: string,
  name: string,
  data: { suspend?: boolean; schedule?: string }
): Promise<ScheduleInfo> {
  return apiRequest<ScheduleInfo>(
    `/namespaces/${namespace}/schedules/${name}`,
    { method: 'PATCH', body: data }
  );
}

export async function triggerSchedule(
  namespace: string,
  name: string
): Promise<{ status: string; job: string; schedule: string }> {
  return apiRequest<{ status: string; job: string; schedule: string }>(
    `/namespaces/${namespace}/schedules/${name}/trigger`,
    { method: 'POST' }
  );
}
