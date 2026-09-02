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
  render(
    <TemplateModal goldenImages={images} projects={PROJECTS} defaultProject="" onClose={onClose} />
  );
  return { onClose };
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
