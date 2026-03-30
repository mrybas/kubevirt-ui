export interface ScheduleInfo {
  name: string;
  namespace: string;
  vm_name: string;
  vm_namespace: string;
  action: string;
  schedule: string;
  suspended: boolean;
  last_schedule_time: string | null;
  active_jobs: number;
  creation_time: string;
}

export interface CreateScheduleRequest {
  name: string;
  action: string;
  schedule: string;
  vm_name: string;
  vm_namespace: string;
  suspend?: boolean;
}
