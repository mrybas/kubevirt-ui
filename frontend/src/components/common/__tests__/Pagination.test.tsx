import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Pagination } from '../Pagination';

describe('Pagination', () => {
  it('renders nothing when totalPages <= 1 and no perPage selector', () => {
    const { container } = render(
      <Pagination page={1} totalPages={1} onPageChange={vi.fn()} />,
    );
    expect(container.innerHTML).toBe('');
  });

  it('renders page number buttons', () => {
    render(<Pagination page={1} totalPages={5} onPageChange={vi.fn()} />);

    for (let i = 1; i <= 5; i++) {
      expect(screen.getByText(String(i))).toBeInTheDocument();
    }
  });

  it('disables Prev/First buttons on first page', () => {
    render(<Pagination page={1} totalPages={5} onPageChange={vi.fn()} />);

    expect(screen.getByTitle('Previous page')).toBeDisabled();
    expect(screen.getByTitle('First page')).toBeDisabled();
  });

  it('disables Next/Last buttons on last page', () => {
    render(<Pagination page={5} totalPages={5} onPageChange={vi.fn()} />);

    expect(screen.getByTitle('Next page')).toBeDisabled();
    expect(screen.getByTitle('Last page')).toBeDisabled();
  });

  it('enables Prev/Next on middle page', () => {
    render(<Pagination page={3} totalPages={5} onPageChange={vi.fn()} />);

    expect(screen.getByTitle('Previous page')).toBeEnabled();
    expect(screen.getByTitle('Next page')).toBeEnabled();
  });

  it('calls onPageChange when page number clicked', async () => {
    const onPageChange = vi.fn();
    render(<Pagination page={1} totalPages={5} onPageChange={onPageChange} />);

    await userEvent.click(screen.getByText('3'));
    expect(onPageChange).toHaveBeenCalledWith(3);
  });

  it('calls onPageChange with page-1 when Prev clicked', async () => {
    const onPageChange = vi.fn();
    render(<Pagination page={3} totalPages={5} onPageChange={onPageChange} />);

    await userEvent.click(screen.getByTitle('Previous page'));
    expect(onPageChange).toHaveBeenCalledWith(2);
  });

  it('calls onPageChange with page+1 when Next clicked', async () => {
    const onPageChange = vi.fn();
    render(<Pagination page={3} totalPages={5} onPageChange={onPageChange} />);

    await userEvent.click(screen.getByTitle('Next page'));
    expect(onPageChange).toHaveBeenCalledWith(4);
  });

  it('renders per-page selector and calls onPerPageChange', async () => {
    const onPerPageChange = vi.fn();
    render(
      <Pagination
        page={1}
        totalPages={1}
        onPageChange={vi.fn()}
        perPage={25}
        onPerPageChange={onPerPageChange}
      />,
    );

    expect(screen.getByText('Per page:')).toBeInTheDocument();
    expect(screen.getByText('25')).toBeInTheDocument();
    expect(screen.getByText('50')).toBeInTheDocument();
    expect(screen.getByText('100')).toBeInTheDocument();

    await userEvent.click(screen.getByText('50'));
    expect(onPerPageChange).toHaveBeenCalledWith(50);
  });

  it('shows "Showing X–Y of Z" text', () => {
    render(
      <Pagination
        page={2}
        totalPages={4}
        onPageChange={vi.fn()}
        perPage={25}
        total={90}
      />,
    );

    // page 2, perPage 25 → Showing 26–50 of 90
    expect(screen.getByText(/Showing 26.50 of 90/)).toBeInTheDocument();
  });

  it('clamps end to total on last page', () => {
    render(
      <Pagination
        page={4}
        totalPages={4}
        onPageChange={vi.fn()}
        perPage={25}
        total={90}
      />,
    );

    // page 4, perPage 25 → Showing 76–90 of 90
    expect(screen.getByText(/Showing 76.90 of 90/)).toBeInTheDocument();
  });

  it('renders ellipsis for many pages', () => {
    render(<Pagination page={5} totalPages={10} onPageChange={vi.fn()} />);

    // pageWindow(5, 10) → [1, '...', 4, 5, 6, '...', 10]
    expect(screen.getByText('1')).toBeInTheDocument();
    expect(screen.getByText('10')).toBeInTheDocument();
    const ellipses = screen.getAllByText('…');
    expect(ellipses).toHaveLength(2);
  });
});
