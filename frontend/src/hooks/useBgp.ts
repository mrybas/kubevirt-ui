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
  listBgpConfs,
  upsertBgpConf,
  deleteBgpConf,
} from '../api/bgp';
import type { SpeakerDeployRequest, AnnouncementRequest, BgpConfRequest } from '../types/bgp';
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

// BgpConf — what an egress gateway's FRR peers with. One shared config serves
// every gateway; see the note in types/bgp.ts.

export function useBgpConfs() {
  return useQuery({
    queryKey: ['bgp-confs'],
    queryFn: listBgpConfs,
    staleTime: 60000,
    // The create-gateway form reads this list, and the usual path is "create a
    // BgpConf, then immediately create the gateway that uses it". Inside the
    // stale window that form opened without the config that had just been
    // made, so the only choice it offered was "No BGP".
    refetchOnMount: 'always',
  });
}

export function useUpsertBgpConf() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (request: BgpConfRequest) => upsertBgpConf(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['bgp-confs'] });
      notify.success('BGP configuration saved');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to save BGP configuration');
    },
  });
}

export function useDeleteBgpConf() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (name: string) => deleteBgpConf(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['bgp-confs'] });
      notify.success('BGP configuration deleted');
    },
    onError: (err: Error) => {
      notify.error(err.message || 'Failed to delete BGP configuration');
    },
  });
}
