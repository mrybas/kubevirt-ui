/**
 * A catalogue selection must materialise at most one disk, however many
 * times the template form is submitted.
 *
 * Before this test existed: pick a catalogue image, submit, the import
 * succeeds but the template create step then fails (409 name conflict,
 * transient 5xx, a permission check) — `goldenImageName` still holds the
 * catalogue selection, the natural next move is to press submit again, and
 * `handleSubmit` re-resolved the same catalogue row and imported it a
 * *second* time. Nothing in the UI said a disk had been created in the
 * background, or that retrying created another one. On a large image that
 * is real storage consumed silently, and the normal response to a failure —
 * press it again — is exactly what caused it.
 *
 * These tests exercise `handleSubmit` itself (via the exported
 * `TemplateModal`), not just the option list `imageOptions.ts` builds —
 * that gap is what let the bug above go unnoticed by
 * `template-image-choice.test.tsx`.
 */
import { useState } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { TemplateModal } from '../VMTemplates';
import type { GoldenImage } from '../../types/template';

const mockCreateTemplate = vi.fn();
const mockUpdateTemplate = vi.fn();
const mockCreateImage = vi.fn();

// A stand-in for react-query's useMutation: mutateAsync delegates to the
// vi.fn() above (so calls/args/counts are inspectable), while `isPending`
// and `error` are tracked with real React state so the component sees them
// update across renders exactly as it would against the real hook — a
// static `{ error: null }` mock would never show the "Failed to create
// template" error the component reads from `createTemplate.error`, which
// is exactly the state this bug lived in.
function useMutationMock(mutateFn: (variables: unknown) => Promise<unknown>) {
  const [state, setState] = useState<{ isPending: boolean; error: unknown }>({
    isPending: false,
    error: null,
  });
  return {
    isPending: state.isPending,
    error: state.error,
    mutateAsync: async (variables: unknown) => {
      setState({ isPending: true, error: null });
      try {
        const result = await mutateFn(variables);
        setState({ isPending: false, error: null });
        return result;
      } catch (err) {
        setState({ isPending: false, error: err });
        throw err;
      }
    },
  };
}

vi.mock('../../hooks/useTemplates', () => ({
  useCreateTemplate: () => useMutationMock(mockCreateTemplate),
  useUpdateTemplate: () => useMutationMock(mockUpdateTemplate),
  useCreateImage: () => useMutationMock(mockCreateImage),
}));

const PROJECTS = [{ name: 'acme-dev', display_name: 'Acme Dev' }];

const CATALOG_IMAGE: GoldenImage = {
  name: 'rocky-9:1',
  namespace: '',
  display_name: 'Rocky 9',
  status: 'Ready',
  disk_type: 'image',
  persistent: false,
  scope: 'environment',
  origin: 'catalog',
  catalog_ref: 'p/rocky-9:1',
};

const CLUSTER_IMAGE: GoldenImage = {
  name: 'ubuntu',
  namespace: 'acme-dev',
  display_name: 'Ubuntu',
  size: '20Gi',
  status: 'Ready',
  disk_type: 'image',
  persistent: false,
  scope: 'environment',
  origin: 'cluster',
};

beforeEach(() => {
  mockCreateTemplate.mockReset();
  mockUpdateTemplate.mockReset();
  mockCreateImage.mockReset();
});

function renderModal(images: GoldenImage[]) {
  const onClose = vi.fn();
  const { rerender } = render(
    <TemplateModal goldenImages={images} projects={PROJECTS} defaultProject="" onClose={onClose} />
  );
  // `goldenImages` is a prop fed by `useGoldenImages()`, which react-query
  // refetches after a successful import. A test that only ever renders one
  // static list can never see what the user sees on the second submit — which
  // is precisely how the dead-end below went unnoticed.
  const refetch = (next: GoldenImage[]) =>
    rerender(
      <TemplateModal goldenImages={next} projects={PROJECTS} defaultProject="" onClose={onClose} />
    );
  return { onClose, refetch };
}

async function fillCommonFields(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByPlaceholderText('ubuntu-medium'), 'my-template');
  await user.type(screen.getByPlaceholderText('Ubuntu Medium'), 'My Template');
}

async function pickFromSelect(
  user: ReturnType<typeof userEvent.setup>,
  triggerText: string,
  optionText: string
) {
  await user.click(screen.getByText(triggerText));
  await user.click(screen.getByText(optionText));
}

async function submit(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: /Create Template/i }));
}

describe('the template form does not re-import an already-materialised catalogue image', () => {
  it('calls the import mutation exactly once across a failed submit and a retry', async () => {
    const user = userEvent.setup();
    mockCreateImage.mockResolvedValue({ name: 'rocky-9-1-abc12' });
    mockCreateTemplate
      .mockRejectedValueOnce(new Error('template "my-template" already exists'))
      .mockResolvedValueOnce({});

    const { onClose } = renderModal([CATALOG_IMAGE]);

    await fillCommonFields(user);
    await pickFromSelect(user, 'Select a project...', 'Acme Dev');
    await pickFromSelect(user, 'Select an image...', 'Rocky 9 (will be imported)');

    // First submit: import succeeds, template create fails.
    await submit(user);
    await waitFor(() => expect(mockCreateTemplate).toHaveBeenCalledTimes(1));
    // The heading text ("Failed to create template:") and the interpolated
    // error message are separate text nodes in the same element — match on
    // a substring that lands entirely inside the error message's own node.
    await screen.findByText(/already exists/i);
    expect(mockCreateImage).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();

    // Retry with the same selection: template create is retried, but the
    // image must NOT be imported a second time.
    await submit(user);
    await waitFor(() => expect(mockCreateTemplate).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));

    expect(mockCreateImage).toHaveBeenCalledTimes(1);

    // Both template-create calls must reference the one disk the single
    // import produced, not the catalogue ref itself.
    expect(mockCreateTemplate.mock.calls[0][0]).toMatchObject({ golden_image_name: 'rocky-9-1-abc12' });
    expect(mockCreateTemplate.mock.calls[1][0]).toMatchObject({ golden_image_name: 'rocky-9-1-abc12' });
  });

  it('retries against the disk the refetch now shows, not the catalogue ref', async () => {
    // What the merged list looks like once the import has landed: the
    // catalogue row is GONE — `merge()` prefers the cluster row and drops its
    // catalogue counterpart — and the disk that replaced it carries the ref as
    // provenance. `goldenImageName` still holds "p/rocky-9:1".
    //
    // Before the fix, `projectImages.find` matched cluster rows on `name`
    // only, so nothing matched: the reuse branch and the import branch were
    // both skipped and "p/rocky-9:1" went out as `golden_image_name`. That is
    // a disk name the backend has never heard of, so the retry 404'd — the
    // "cannot import twice" property held only because the user dead-ended.
    const MATERIALISED: GoldenImage = {
      name: 'rocky-9-1-abc12',
      namespace: 'acme-dev',
      display_name: 'Rocky 9',
      size: '20Gi',
      status: 'Ready',
      disk_type: 'image',
      persistent: false,
      scope: 'environment',
      origin: 'cluster',
      catalog_ref: 'p/rocky-9:1',
    };

    const user = userEvent.setup();
    mockCreateImage.mockResolvedValue({ name: 'rocky-9-1-abc12' });
    mockCreateTemplate
      .mockRejectedValueOnce(new Error('template "my-template" already exists'))
      .mockResolvedValueOnce({});

    const { onClose, refetch } = renderModal([CATALOG_IMAGE]);

    await fillCommonFields(user);
    await pickFromSelect(user, 'Select a project...', 'Acme Dev');
    await pickFromSelect(user, 'Select an image...', 'Rocky 9 (will be imported)');

    await submit(user);
    await waitFor(() => expect(mockCreateTemplate).toHaveBeenCalledTimes(1));
    await screen.findByText(/already exists/i);

    // The import succeeded, so the list the modal is given now reflects it.
    refetch([MATERIALISED]);

    await submit(user);
    await waitFor(() => expect(mockCreateTemplate).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));

    // Still exactly one import, and — the part that was broken — the retry
    // references the disk, never the raw catalogue ref.
    expect(mockCreateImage).toHaveBeenCalledTimes(1);
    expect(mockCreateTemplate.mock.calls[1][0]).toMatchObject({
      golden_image_name: 'rocky-9-1-abc12',
    });
  });

  // REMOVED: 'picks a merged cluster row up by its catalogue ref on a fresh
  // submit'. It passed unchanged against the pre-fix code and so proved
  // nothing. `imageOptions.ts` gives a CLUSTER row its disk name as the
  // option's value, so picking one from the select can never put a catalogue
  // ref into `goldenImageName` — the old `img.name ===` branch matched and the
  // `catalog_ref` branch it claimed to cover was never reached. The only way
  // `goldenImageName` holds a ref while the list shows the merged cluster row
  // is the refetch above, which is where that branch is actually exercised;
  // the by-disk-name path is covered by the cluster-row test below.

  it('never attempts template creation when the import itself fails, and surfaces the failure', async () => {
    const user = userEvent.setup();
    mockCreateImage.mockRejectedValue(new Error('registry unreachable'));

    renderModal([CATALOG_IMAGE]);

    await fillCommonFields(user);
    await pickFromSelect(user, 'Select a project...', 'Acme Dev');
    await pickFromSelect(user, 'Select an image...', 'Rocky 9 (will be imported)');

    await submit(user);

    await screen.findByText(/registry unreachable/i);
    expect(mockCreateTemplate).not.toHaveBeenCalled();
  });

  it('imports with catalog_ref, never with the host-less ref as source_registry', async () => {
    // The sibling of Storage.tsx's call site, and it carried the same defect.
    // `catalog_ref` has no registry host in it by design, so sent as
    // `source_registry` the import resolves against Docker Hub and the disk
    // that results never re-joins its catalogue row. Asserting the mutation
    // was CALLED — which is all the tests above do — could not see that.
    const user = userEvent.setup();
    mockCreateImage.mockResolvedValue({ name: 'rocky-9-1-abc12' });
    mockCreateTemplate.mockResolvedValue({});

    renderModal([CATALOG_IMAGE]);

    await fillCommonFields(user);
    await pickFromSelect(user, 'Select a project...', 'Acme Dev');
    await pickFromSelect(user, 'Select an image...', 'Rocky 9 (will be imported)');
    await submit(user);

    await waitFor(() => expect(mockCreateImage).toHaveBeenCalledTimes(1));
    const { data, namespace } = mockCreateImage.mock.calls[0][0] as {
      data: Record<string, unknown>;
      namespace: string;
    };

    expect(data.catalog_ref).toBe('p/rocky-9:1');
    expect(data.source_registry).toBeUndefined();
    // No credential names in the browser — the backend attaches the tenant's
    // robot Secret and CA by convention.
    expect(data.source_registry_secret).toBeUndefined();
    expect(data.source_registry_ca_configmap).toBeUndefined();
    expect(namespace).toBe('acme-dev');
  });

  it('never calls the import mutation for a cluster-row selection', async () => {
    const user = userEvent.setup();
    mockCreateTemplate.mockResolvedValue({});

    const { onClose } = renderModal([CLUSTER_IMAGE]);

    await fillCommonFields(user);
    await pickFromSelect(user, 'Select a project...', 'Acme Dev');
    await pickFromSelect(user, 'Select an image...', 'Ubuntu (20Gi)');

    await submit(user);

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(mockCreateImage).not.toHaveBeenCalled();
    expect(mockCreateTemplate.mock.calls[0][0]).toMatchObject({ golden_image_name: 'ubuntu' });
  });
});
