/**
 * A failed batch says why, not just how many.
 *
 * Creating a VM from a template written as a ManagedVMTemplate answered
 *
 *     404 Template op-ubuntu-small not found
 *
 * and the wizard showed "Failed to create 1 VM(s): op-vm1" and nothing else.
 * The reason existed, in a sentence, and was discarded in the catch one line
 * before it could be rendered — the same pattern as the two error messages
 * fixed before it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { CreateVMWizard } from '../CreateVMWizard';

const mutateAsync = vi.fn();

vi.mock('@/hooks/useTemplates', () => ({
  useTemplates: () => ({ data: { items: [] }, isLoading: false }),
  useGoldenImages: () => ({ data: { items: [] } }),
  useCreateVMFromTemplate: () => ({ mutateAsync, isPending: false, error: null }),
}));
vi.mock('@/hooks/useVpcs', () => ({ useVpcs: () => ({ data: { items: [] } }) }));
vi.mock('@/hooks/useNetwork', () => ({ useSubnets: () => ({ data: [] }) }));
vi.mock('@/hooks/useStorage', () => ({ useStorageClasses: () => ({ data: { items: [] } }) }));
vi.mock('@/hooks/useFolders', () => ({ useFoldersFlat: () => ({ data: { items: [] } }) }));
vi.mock('@/components/vm/SSHKeyPicker', () => ({ SSHKeyPicker: () => null }));

const template = {
  name: 'op-ubuntu-small',
  display_name: 'Op Ubuntu Small',
  category: 'linux',
  os_type: 'ubuntu',
  golden_image_name: 'ubuntu-2404',
  golden_image_namespace: 'golden-images',
  compute: { cpu_cores: 2, cpu_sockets: 1, cpu_threads: 1, memory: '4Gi' },
  disk: { size: '20Gi' },
  network: { type: 'default' as const },
};

describe('a batch that fails', () => {
  beforeEach(() => {
    mutateAsync.mockReset();
    mutateAsync.mockRejectedValue(new Error('Template op-ubuntu-small not found'));
  });

  it('shows the reason the server gave, beside the name', async () => {
    render(
      <CreateVMWizard
        projects={[{ name: 'default', display_name: 'Default' }]}
        defaultProject="default"
        defaultTemplate={template as never}
        onClose={() => {}}
        onSuccess={() => {}}
      />,
    );

    const user = userEvent.setup();
    await user.clear(screen.getByPlaceholderText(/Web Server/i));
    await user.type(screen.getByPlaceholderText(/Web Server/i), 'op-vm1');
    await user.click(screen.getByText('Next'));   // customize → network
    await user.click(screen.getByText('Next'));   // network → cloudInit
    await user.click(screen.getByText('Next'));   // cloudInit → review
    await user.click(screen.getByText(/Create VM/i));

    await waitFor(() => {
      expect(screen.getByText(/Template op-ubuntu-small not found/)).toBeTruthy();
    });
  });
});
