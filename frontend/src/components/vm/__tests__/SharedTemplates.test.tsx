/**
 * The template step offers templates whose image is in the selected project or
 * in a shared image namespace, and nothing else.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CreateVMWizard } from '../CreateVMWizard';

const template = (name: string, ns: string, image: string) => ({
  name,
  display_name: name,
  category: 'linux',
  os_type: 'linux',
  golden_image_name: image,
  golden_image_namespace: ns,
  compute: { cpu_cores: 1, cpu_sockets: 1, cpu_threads: 1, memory: '2Gi' },
  disk: { size: '10Gi' },
  network: { type: 'default' as const },
});

const image = (name: string, namespace: string, shared = false) => ({
  name, namespace, shared, status: 'Ready', disk_type: 'image', persistent: false, scope: 'environment',
});

vi.mock('@/hooks/useTemplates', () => ({
  useTemplates: () => ({
    data: {
      items: [
        template('own-template', 'tenant-mine', 'own-image'),
        template('shared-template', 'vm-images', 'nixos'),
        template('other-project-template', 'tenant-other', 'other-image'),
      ],
    },
    isLoading: false,
  }),
  useGoldenImages: () => ({
    data: {
      items: [
        image('own-image', 'tenant-mine'),
        image('nixos', 'vm-images', true),
        image('other-image', 'tenant-other'),
      ],
    },
  }),
  useCreateVMFromTemplate: () => ({ mutateAsync: vi.fn(), isPending: false, error: null }),
}));
vi.mock('@/hooks/useVpcs', () => ({ useVpcs: () => ({ data: { items: [] } }) }));
vi.mock('@/hooks/useNetwork', () => ({ useSubnets: () => ({ data: [] }) }));
vi.mock('@/hooks/useStorage', () => ({ useStorageClasses: () => ({ data: { items: [] } }) }));
vi.mock('@/hooks/useFolders', () => ({ useFoldersFlat: () => ({ data: { items: [] } }) }));
vi.mock('@/components/vm/SSHKeyPicker', () => ({ SSHKeyPicker: () => null }));

describe('CreateVMWizard template step', () => {
  it('offers own and shared templates, not another project\'s', () => {
    render(
      <CreateVMWizard
        projects={[
          { name: 'tenant-mine', display_name: 'Mine' },
          { name: 'tenant-other', display_name: 'Other' },
        ]}
        defaultProject="tenant-mine"
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );
    expect(screen.getAllByText('own-template').length).toBeGreaterThan(0);
    expect(screen.getAllByText('shared-template').length).toBeGreaterThan(0);
    expect(screen.queryByText('other-project-template')).toBeNull();
  });
});
