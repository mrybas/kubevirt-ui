/**
 * VirtualMachines list page — Phase 2 display_name tests.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { VirtualMachines } from '../VirtualMachines';

// ─── mock captured args ───────────────────────────────────────────────────────

const useVMsMock = vi.fn();

// ─── mocks ────────────────────────────────────────────────────────────────────

vi.mock('@/hooks/useVMs', () => ({
  useVMs: (...args: any[]) => useVMsMock(...args),
  useStartVM: () => ({ mutate: vi.fn() }),
  useStopVM: () => ({ mutate: vi.fn() }),
  useRestartVM: () => ({ mutate: vi.fn() }),
  useDeleteVM: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock('@/hooks/useNamespaces', () => ({
  useNamespaces: () => ({ data: { items: [] } }),
}));

vi.mock('@/store', () => ({
  useAppStore: () => ({ selectedNamespace: null }),
}));

vi.mock('@/hooks/useFolders', () => ({
  useFoldersFlat: () => ({ data: { items: [] } }),
}));

vi.mock('@/hooks/usePagination', () => ({
  usePagination: () => ({ page: 1, perPage: 50, setPage: vi.fn(), setPerPage: vi.fn() }),
}));

vi.mock('@/components/vm/CreateVMWizard', () => ({
  CreateVMWizard: () => null,
}));

// ─── fixtures ─────────────────────────────────────────────────────────────────

const mockVM = {
  name: 'web-server-q4r8z',
  display_name: 'Web Server (prod)',
  namespace: 'default',
  status: 'Running',
  ready: true,
  labels: {},
  annotations: {},
  conditions: [],
  volumes: [],
  disks: [],
};

function defaultUseVMsReturn(items = [mockVM]) {
  return {
    data: { items, total: items.length },
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <VirtualMachines />
    </MemoryRouter>
  );
}

// ─── tests ────────────────────────────────────────────────────────────────────

describe('VirtualMachines list – display_name rendering', () => {
  beforeEach(() => {
    useVMsMock.mockReturnValue(defaultUseVMsReturn());
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('shows display_name as primary text in the Name column', () => {
    renderPage();
    expect(screen.getByText('Web Server (prod)')).toBeInTheDocument();
  });

  it('shows K8s name as muted subtitle', () => {
    renderPage();
    // The subtitle contains "name · namespace"
    expect(screen.getByText(/web-server-q4r8z/)).toBeInTheDocument();
  });

  it('subtitle contains namespace alongside K8s name', () => {
    renderPage();
    expect(screen.getByText(/web-server-q4r8z\s*·\s*default/)).toBeInTheDocument();
  });

  it('navigation link uses K8s name (not display_name)', () => {
    renderPage();
    const link = screen.getByRole('link', { name: /Web Server \(prod\)/i });
    expect(link).toHaveAttribute('href', '/vms/default/web-server-q4r8z');
  });

  it('falls back to name when display_name is empty', () => {
    useVMsMock.mockReturnValue(defaultUseVMsReturn([{ ...mockVM, display_name: '' }]));
    renderPage();
    // Should show name since display_name is empty
    const nameLinks = screen.getAllByRole('link');
    const vmLink = nameLinks.find(l => l.getAttribute('href')?.includes('web-server-q4r8z'));
    expect(vmLink?.textContent).toBe('web-server-q4r8z');
  });
});

describe('VirtualMachines list – search debouncing', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useVMsMock.mockReturnValue(defaultUseVMsReturn());
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it('passes search param to useVMs after 600ms total debounce', async () => {
    renderPage();

    // Find the DataTable search input
    const searchInput = screen.getByPlaceholderText('Search VMs by display name...');

    // Simulate typing — DataTable debounces 300ms then calls handleSearch,
    // which in turn debounces another 300ms before updating debouncedSearch
    fireEvent.change(searchInput, { target: { value: 'web' } });

    // Before debounce fires — useVMs should NOT have been called with search='web'
    const callsBefore = useVMsMock.mock.calls;
    const hadSearchBefore = callsBefore.some(call => call[3] === 'web');
    expect(hadSearchBefore).toBe(false);

    // Advance DataTable's internal 300ms debounce
    act(() => { vi.advanceTimersByTime(300); });
    // Advance VirtualMachines' internal 300ms debounce
    act(() => { vi.advanceTimersByTime(300); });

    // Now useVMs should have been called with search='web'
    const lastCall = useVMsMock.mock.calls[useVMsMock.mock.calls.length - 1];
    expect(lastCall[3]).toBe('web');
  });

  it('does NOT fire backend call before 600ms debounce window', () => {
    renderPage();
    const searchInput = screen.getByPlaceholderText('Search VMs by display name...');

    const callCountBefore = useVMsMock.mock.calls.length;
    fireEvent.change(searchInput, { target: { value: 'web' } });

    // Advance only 100ms — neither debounce has fired yet
    act(() => { vi.advanceTimersByTime(100); });

    // useVMs may be called during initial render but not with 'web'
    const newCalls = useVMsMock.mock.calls.slice(callCountBefore);
    expect(newCalls.every(call => call[3] !== 'web')).toBe(true);
  });
});
