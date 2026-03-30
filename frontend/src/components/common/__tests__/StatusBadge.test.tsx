import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { StatusBadge } from '../StatusBadge';

describe('StatusBadge', () => {
  it('renders the status text', () => {
    render(<StatusBadge status="Running" />);
    expect(screen.getByText('Running')).toBeInTheDocument();
  });

  it('applies badge-success class for Running', () => {
    render(<StatusBadge status="Running" />);
    expect(screen.getByText('Running')).toHaveClass('badge-success');
  });

  it('applies badge-error class for Failed', () => {
    render(<StatusBadge status="Failed" />);
    expect(screen.getByText('Failed')).toHaveClass('badge-error');
  });

  it('applies badge-warning class for Paused', () => {
    render(<StatusBadge status="Paused" />);
    expect(screen.getByText('Paused')).toHaveClass('badge-warning');
  });

  it('applies badge-info class for Migrating', () => {
    render(<StatusBadge status="Migrating" />);
    expect(screen.getByText('Migrating')).toHaveClass('badge-info');
  });

  it('applies badge-neutral for unknown status', () => {
    render(<StatusBadge status="SomethingElse" />);
    expect(screen.getByText('SomethingElse')).toHaveClass('badge-neutral');
  });

  it('applies additional className', () => {
    render(<StatusBadge status="Running" className="extra" />);
    const el = screen.getByText('Running');
    expect(el).toHaveClass('badge-success');
    expect(el).toHaveClass('extra');
  });
});
