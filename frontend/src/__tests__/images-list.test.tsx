import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ImageRows } from '../components/images/ImageRows';

describe('the image list', () => {
  it('marks a catalogue entry as not yet materialised', () => {
    render(
      <ImageRows
        items={[{ name: 'rocky-9:1', origin: 'catalog', status: 'Catalog' }]}
        catalogAvailable
      />
    );
    expect(screen.getByText(/rocky-9:1/)).toBeInTheDocument();
    expect(screen.getByTestId('origin-catalog')).toBeInTheDocument();
  });

  it('warns when the catalogue could not be read, without hiding local disks', () => {
    render(
      <ImageRows
        items={[{ name: 'ubuntu', origin: 'cluster', status: 'Ready' }]}
        catalogAvailable={false}
      />
    );
    expect(screen.getByText(/ubuntu/)).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(/catalog/i);
  });
});
