import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import * as scheduleApi from '@/api/schedules';

export function useSchedules(namespace: string, vmName?: string) {
  return useQuery({
    queryKey: ['schedules', namespace, vmName || 'all'],
    queryFn: () => scheduleApi.listSchedules(namespace, vmName),
    enabled: !!namespace,
  });
}

export function useCreateSchedule() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      data,
    }: {
      namespace: string;
      data: scheduleApi.CreateScheduleRequest;
    }) => scheduleApi.createSchedule(namespace, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['schedules', variables.namespace] });
    },
  });
}

export function useDeleteSchedule() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      name,
    }: {
      namespace: string;
      name: string;
    }) => scheduleApi.deleteSchedule(namespace, name),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['schedules', variables.namespace] });
    },
  });
}

export function useUpdateSchedule() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      name,
      data,
    }: {
      namespace: string;
      name: string;
      data: { suspend?: boolean; schedule?: string };
    }) => scheduleApi.updateSchedule(namespace, name, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['schedules', variables.namespace] });
    },
  });
}

export function useTriggerSchedule() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      namespace,
      name,
    }: {
      namespace: string;
      name: string;
    }) => scheduleApi.triggerSchedule(namespace, name),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['schedules', variables.namespace] });
    },
  });
}
