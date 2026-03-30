import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ConfirmDeleteModal } from '../ConfirmDeleteModal';

const baseProps = {
  isOpen: true,
  onClose: vi.fn(),
  onConfirm: vi.fn(),
  resourceName: 'my-vm',
  resourceType: 'VM',
};

describe('ConfirmDeleteModal', () => {
  it('renders nothing when isOpen is false', () => {
    const { container } = render(
      <ConfirmDeleteModal {...baseProps} isOpen={false} />,
    );
    expect(container.innerHTML).toBe('');
  });

  it('renders resource name and type', () => {
    render(<ConfirmDeleteModal {...baseProps} />);
    expect(screen.getByText('Delete VM')).toBeInTheDocument();
    expect(screen.getByText('my-vm')).toBeInTheDocument();
  });

  it('calls onConfirm when Delete clicked (no typing required)', async () => {
    const onConfirm = vi.fn();
    render(<ConfirmDeleteModal {...baseProps} onConfirm={onConfirm} />);

    await userEvent.click(screen.getByText('Delete'));
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it('calls onClose when Cancel clicked', async () => {
    const onClose = vi.fn();
    render(<ConfirmDeleteModal {...baseProps} onClose={onClose} />);

    await userEvent.click(screen.getByText('Cancel'));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('disables Delete button until name is typed when requireTyping', () => {
    render(<ConfirmDeleteModal {...baseProps} requireTyping />);

    const deleteBtn = screen.getByText('Delete');
    expect(deleteBtn).toBeDisabled();
  });

  it('enables Delete button after typing correct name', async () => {
    render(<ConfirmDeleteModal {...baseProps} requireTyping />);

    const input = screen.getByPlaceholderText('my-vm');
    await userEvent.type(input, 'my-vm');

    expect(screen.getByText('Delete')).toBeEnabled();
  });

  it('keeps Delete disabled with partial typing', async () => {
    render(<ConfirmDeleteModal {...baseProps} requireTyping />);

    const input = screen.getByPlaceholderText('my-vm');
    await userEvent.type(input, 'my-v');

    expect(screen.getByText('Delete')).toBeDisabled();
  });

  it('shows Deleting... text when isDeleting', () => {
    render(<ConfirmDeleteModal {...baseProps} isDeleting />);

    expect(screen.getByText('Deleting...')).toBeInTheDocument();
    expect(screen.getByText('Deleting...')).toBeDisabled();
  });
});
