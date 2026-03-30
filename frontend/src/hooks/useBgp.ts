/**
 * React Query hooks for BGP speaker management.
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  getSpeakerStatus,
  deploySpeaker,
  updateSpeaker,
  deleteSpeaker,
  listAnnouncements,
  createAnnouncement,
  deleteAnnouncement,
  listBgpSessions,
} from '../api/bgp';
import type { SpeakerDeployRequest, AnnouncementRequest } from '../types/bgp';
import { notify } from '../store/notifications';

export function useSpeakerStatus() {
  return useQuery({
    queryKey: ['bgp-speaker'],
    queryFn: getSpeakerStatus,
    refetchInterval: 15000,
  });
}

export function useDeploySpeaker() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: SpeakerDeployRequest) => deploySpeaker(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['bgp-speaker'] });
      notify.success('BGP speaker deployed');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to deploy BGP speaker');
    },
  });
}

export function useUpdateSpeaker() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: SpeakerDeployRequest) => updateSpeaker(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['bgp-speaker'] });
      notify.success('BGP speaker updated');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to update BGP speaker');
    },
  });
}

export function useDeleteSpeaker() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => deleteSpeaker(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['bgp-speaker'] });
      queryClient.invalidateQueries({ queryKey: ['bgp-sessions'] });
      notify.success('BGP speaker removed');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to remove BGP speaker');
    },
  });
}

export function useAnnouncements() {
  return useQuery({
    queryKey: ['bgp-announcements'],
    queryFn: listAnnouncements,
    refetchInterval: 15000,
  });
}

export function useCreateAnnouncement() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: AnnouncementRequest) => createAnnouncement(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['bgp-announcements'] });
      notify.success('BGP announcement added');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to add BGP announcement');
    },
  });
}

export function useDeleteAnnouncement() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: AnnouncementRequest) => deleteAnnouncement(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['bgp-announcements'] });
      notify.success('BGP announcement removed');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to remove BGP announcement');
    },
  });
}

export function useBgpSessions() {
  return useQuery({
    queryKey: ['bgp-sessions'],
    queryFn: listBgpSessions,
    refetchInterval: 10000,
  });
}
