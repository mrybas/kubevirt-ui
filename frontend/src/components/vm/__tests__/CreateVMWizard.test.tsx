/**
 * CreateVMWizard — Phase 2 display_name tests.
 * Renders starting at 'customize' step via defaultTemplate prop.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { CreateVMWizard } from '../CreateVMWizard';

// ─── mocks ────────────────────────────────────────────────────────────────────

const mockMutateAsync = vi.fn().mockResolvedValue({ name: 'web-server-q4r8z', display_name: 'Web Server' });

vi.mock('@/hooks/useTemplates', () => ({
  useTemplates: () => ({ data: { items: [] }, isLoading: false }),
  useGoldenImages: () => ({ data: { items: [] } }),
  useCreateVMFromTemplate: () => ({
    mutateAsync: mockMutateAsync,
    isPending: false,
    error: null,
  }),
}));

vi.mock('@/hooks/useNetwork', () => ({
  useSubnets: () => ({ data: [] }),
}));

vi.mock('@/hooks/useFolders', () => ({
  useFoldersFlat: () => ({ data: { items: [] } }),
}));

vi.mock('@/components/vm/SSHKeyPicker', () => ({
  SSHKeyPicker: () => null,
}));

// ─── fixtures ─────────────────────────────────────────────────────────────────

const mockTemplate = {
  name: 'ubuntu-22-04',
  display_name: 'Ubuntu 22.04',
  category: 'linux',
  os_type: 'ubuntu',
  golden_image_name: 'ubuntu-22-04',
  golden_image_namespace: 'default',
  compute: { cpu_cores: 2, cpu_sockets: 1, cpu_threads: 1, memory: '4Gi' },
  disk: { size: '20Gi' },
  network: { type: 'default' as const },
};

const defaultProps = {
  projects: [{ name: 'default', display_name: 'Default' }],
  defaultProject: 'default',
  defaultTemplate: mockTemplate,
  onClose: vi.fn(),
  onSuccess: vi.fn(),
};

// ─── helpers ──────────────────────────────────────────────────────────────────

function getDisplayNameInput() {
  return screen.getByPlaceholderText(/Web Server/i);
}

async function advanceToReview(user: ReturnType<typeof userEvent.setup>, displayName: string) {
  await user.clear(getDisplayNameInput());
  await user.type(getDisplayNameInput(), displayName);
  // customize → network
  await user.click(screen.getByText('Next'));
  // network → cloudInit
  await user.click(screen.getByText('Next'));
  // cloudInit → review
  await user.click(screen.getByText('Next'));
}

// ─── tests ────────────────────────────────────────────────────────────────────

describe('CreateVMWizard – Display Name field', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders "Display Name" label', () => {
    render(<CreateVMWizard {...defaultProps} />);
    expect(screen.getByText('Display Name *')).toBeInTheDocument();
  });

  it('renders placeholder "e.g. Web Server (prod)"', () => {
    render(<CreateVMWizard {...defaultProps} />);
    expect(screen.getByPlaceholderText(/e\.g\. Web Server \(prod\)/i)).toBeInTheDocument();
  });

  it('renders helper text about auto-generated K8s name', () => {
    render(<CreateVMWizard {...defaultProps} />);
    expect(screen.getByText(/K8s resource name will be auto-generated/i)).toBeInTheDocument();
  });

  it('Next button disabled when display_name is empty', () => {
    render(<CreateVMWizard {...defaultProps} />);
    const nextBtn = screen.getByText('Next');
    expect(nextBtn).toBeDisabled();
  });

  it('Next button enabled for any non-empty text (no DNS-1123 restriction)', async () => {
    const user = userEvent.setup();
    render(<CreateVMWizard {...defaultProps} />);
    await user.type(getDisplayNameInput(), 'Web Server (prod)');
    expect(screen.getByText('Next')).toBeEnabled();
  });

  it('Next button enabled for name with spaces and special chars', async () => {
    const user = userEvent.setup();
    render(<CreateVMWizard {...defaultProps} />);
    await user.type(getDisplayNameInput(), 'My VM #1 @ prod!');
    expect(screen.getByText('Next')).toBeEnabled();
  });

  it('accepts up to 100 characters', async () => {
    const user = userEvent.setup();
    render(<CreateVMWizard {...defaultProps} />);
    const input = getDisplayNameInput();
    const hundredChars = 'a'.repeat(100);
    await user.type(input, hundredChars);
    expect((input as HTMLInputElement).value).toBe(hundredChars);
  });

  it('does not accept more than 100 characters', async () => {
    const user = userEvent.setup();
    render(<CreateVMWizard {...defaultProps} />);
    const input = getDisplayNameInput();
    // Type 101 chars — onChange guards with length <= 100
    const hundredAndOne = 'a'.repeat(101);
    fireEvent.change(input, { target: { value: hundredAndOne } });
    // Should still be at most 100
    expect((input as HTMLInputElement).value.length).toBeLessThanOrEqual(100);
  });
});

describe('CreateVMWizard – submit payload uses display_name', () => {
  it('sends display_name (not name) in the request', async () => {
    const user = userEvent.setup();
    render(<CreateVMWizard {...defaultProps} />);
    await advanceToReview(user, 'Web Server');
    await user.click(screen.getByText(/Create VM/i));

    expect(mockMutateAsync).toHaveBeenCalledWith({
      namespace: 'default',
      data: expect.objectContaining({
        display_name: 'Web Server',
        template_name: 'ubuntu-22-04',
      }),
    });
    expect(mockMutateAsync).toHaveBeenCalledWith(
      expect.objectContaining({
        data: expect.not.objectContaining({ name: expect.anything() }),
      })
    );
  });
});
