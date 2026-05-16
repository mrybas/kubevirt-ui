/**
 * VMDetail page — Phase 2 display_name tests:
 * H1, subtitle, edit modal, document.title, queryClient invalidation.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { VMDetail } from '../VMDetail';

// ─── mocks ────────────────────────────────────────────────────────────────────

const mockUpdateVMDisplayName = vi.fn();

vi.mock('@/hooks/useVMs', () => ({
  useVM: () => ({
    data: mockVM,
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  }),
  useStartVM: () => ({ mutate: vi.fn(), isPending: false }),
  useStopVM: () => ({ mutate: vi.fn(), isPending: false }),
  useRestartVM: () => ({ mutate: vi.fn(), isPending: false }),
  useMigrateVM: () => ({ mutate: vi.fn() }),
  useVMYaml: () => ({ data: undefined }),
  useUpdateVM: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useDeleteVM: () => ({ mutate: vi.fn(), isPending: false }),
  useRecreateVM: () => ({ mutate: vi.fn() }),
  useCloneVM: () => ({ mutate: vi.fn() }),
  useResizeVM: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useUpdateVMDisplayName: () => ({
    mutate: mockUpdateVMDisplayName,
    isPending: false,
    error: null,
  }),
}));

// Suppress heavy tab content — we only care about the header in these tests
vi.mock('@/components/vm/tabs', () => ({
  OverviewTab: () => <div>OverviewTab</div>,
  ConsoleTab: () => <div>ConsoleTab</div>,
  DisksTab: () => <div>DisksTab</div>,
  NetworkTab: () => <div>NetworkTab</div>,
  EventsTab: () => <div>EventsTab</div>,
  YamlTab: () => <div>YamlTab</div>,
  SnapshotsTab: () => <div>SnapshotsTab</div>,
  ScheduleTab: () => <div>ScheduleTab</div>,
}));

vi.mock('@/components/vm/EditVMModal', () => ({
  EditVMModal: () => null,
}));

vi.mock('@/components/vm/MigrateVMModal', () => ({
  MigrateVMModal: () => null,
}));

vi.mock('@/components/charts/VMMetricsPanel', () => ({
  default: () => <div>MetricsPanel</div>,
}));

vi.mock('@/store/notifications', () => ({
  notify: { success: vi.fn(), error: vi.fn() },
}));

// ─── fixture ──────────────────────────────────────────────────────────────────

const mockVM = {
  name: 'web-server-q4r8z',
  display_name: 'Web Server (prod)',
  namespace: 'default',
  status: 'Running',
  ready: true,
  node: 'worker-1',
  labels: {},
  annotations: {},
  conditions: [],
  volumes: [],
  disks: [],
};

// ─── wrapper ──────────────────────────────────────────────────────────────────

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={['/vms/default/web-server-q4r8z']}>
      <Routes>
        <Route path="/vms/:namespace/:name" element={<VMDetail />} />
      </Routes>
    </MemoryRouter>
  );
}

// ─── tests ────────────────────────────────────────────────────────────────────

describe('VMDetail – header', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('H1 shows display_name', () => {
    renderDetail();
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Web Server (prod)');
  });

  it('subtitle shows "Resource: {K8s name}"', () => {
    renderDetail();
    expect(screen.getByText(/Resource:/)).toBeInTheDocument();
    expect(screen.getByText(/web-server-q4r8z/)).toBeInTheDocument();
  });

  it('pencil icon button is present next to H1', () => {
    renderDetail();
    expect(screen.getByTitle('Edit display name')).toBeInTheDocument();
  });

  it('clipboard copy button for K8s name is present', () => {
    renderDetail();
    expect(screen.getByTitle('Copy K8s name')).toBeInTheDocument();
  });
});

describe('VMDetail – document.title', () => {
  const originalTitle = document.title;

  afterEach(() => {
    document.title = originalTitle;
  });

  it('sets document.title to display_name', () => {
    renderDetail();
    expect(document.title).toBe('Web Server (prod)');
  });

  it('restores previous title on unmount', () => {
    document.title = 'Previous Page';
    const { unmount } = renderDetail();
    expect(document.title).toBe('Web Server (prod)');
    unmount();
    expect(document.title).toBe('Previous Page');
  });
});

describe('VMDetail – edit display_name modal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('modal is closed initially', () => {
    renderDetail();
    expect(screen.queryByText('Edit Display Name')).not.toBeInTheDocument();
  });

  it('clicking pencil opens edit modal', async () => {
    const user = userEvent.setup();
    renderDetail();
    await user.click(screen.getByTitle('Edit display name'));
    expect(screen.getByText('Edit Display Name')).toBeInTheDocument();
  });

  it('modal input is pre-filled with current display_name', async () => {
    const user = userEvent.setup();
    renderDetail();
    await user.click(screen.getByTitle('Edit display name'));
    const input = screen.getByDisplayValue('Web Server (prod)');
    expect(input).toBeInTheDocument();
  });

  it('pressing Escape closes the modal', async () => {
    const user = userEvent.setup();
    renderDetail();
    await user.click(screen.getByTitle('Edit display name'));
    expect(screen.getByText('Edit Display Name')).toBeInTheDocument();

    const input = screen.getByDisplayValue('Web Server (prod)');
    await user.type(input, '{Escape}');
    expect(screen.queryByText('Edit Display Name')).not.toBeInTheDocument();
  });

  it('pressing Enter calls updateVMDisplayName with the input value', async () => {
    const user = userEvent.setup();
    renderDetail();
    await user.click(screen.getByTitle('Edit display name'));

    const input = screen.getByDisplayValue('Web Server (prod)');
    await user.clear(input);
    await user.type(input, 'New Name{Enter}');

    expect(mockUpdateVMDisplayName).toHaveBeenCalledWith(
      expect.objectContaining({
        namespace: 'default',
        name: 'web-server-q4r8z',
        data: { display_name: 'New Name' },
      }),
      expect.any(Object)
    );
  });

  it('clicking Cancel closes modal without calling mutation', async () => {
    const user = userEvent.setup();
    renderDetail();
    await user.click(screen.getByTitle('Edit display name'));
    await user.click(screen.getByText('Cancel'));

    expect(screen.queryByText('Edit Display Name')).not.toBeInTheDocument();
    expect(mockUpdateVMDisplayName).not.toHaveBeenCalled();
  });

  it('clicking Save calls updateVMDisplayName', async () => {
    const user = userEvent.setup();
    renderDetail();
    await user.click(screen.getByTitle('Edit display name'));

    const input = screen.getByDisplayValue('Web Server (prod)');
    await user.clear(input);
    await user.type(input, 'Updated Name');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(mockUpdateVMDisplayName).toHaveBeenCalledWith(
      expect.objectContaining({
        data: { display_name: 'Updated Name' },
      }),
      expect.any(Object)
    );
  });
});
