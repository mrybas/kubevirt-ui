import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiRequest, ApiError } from '../client';
import { useAuthStore } from '../../store/auth';

describe('apiRequest', () => {
  const mockFetch = vi.fn();

  beforeEach(() => {
    vi.stubGlobal('fetch', mockFetch);
    useAuthStore.setState({
      accessToken: null,
      isAuthenticated: false,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('sends GET request to /api/v1 + endpoint', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ data: 'test' }),
    });

    await apiRequest('/nodes');

    expect(mockFetch).toHaveBeenCalledWith(
      '/api/v1/nodes',
      expect.objectContaining({ method: 'GET' }),
    );
  });

  it('injects Authorization header when token is set', async () => {
    useAuthStore.setState({ accessToken: 'tok-123', isAuthenticated: true });

    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({}),
    });

    await apiRequest('/vms');

    const headers = mockFetch.mock.calls[0][1].headers;
    expect(headers).toEqual(
      expect.objectContaining({ Authorization: 'Bearer tok-123' }),
    );
  });

  it('does not include Authorization header when no token', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({}),
    });

    await apiRequest('/vms');

    const headers = mockFetch.mock.calls[0][1].headers;
    expect(headers).not.toHaveProperty('Authorization');
  });

  it('throws ApiError on non-ok response', async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 403,
      statusText: 'Forbidden',
      json: () => Promise.resolve({ detail: 'Not allowed' }),
    });

    await expect(apiRequest('/secret')).rejects.toThrow(ApiError);

    const error = await apiRequest('/secret').catch((e) => e);
    expect(error).toMatchObject({
      status: 403,
      message: 'Not allowed',
    });
  });

  it('returns empty object for 204 No Content', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 204,
      json: () => Promise.reject(new Error('no body')),
    });

    const result = await apiRequest('/vms/123');
    expect(result).toEqual({});
  });

  it('sends JSON body for POST requests', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ id: '1' }),
    });

    await apiRequest('/vms', { method: 'POST', body: { name: 'vm1' } });

    const [, config] = mockFetch.mock.calls[0];
    expect(config.method).toBe('POST');
    expect(config.body).toBe(JSON.stringify({ name: 'vm1' }));
  });
});
