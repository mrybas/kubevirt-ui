/**
 * The catalogue-unavailable banner spans the table with `colSpan`, which
 * only reads correctly if it matches the number of `<th>` columns in the
 * header. Storage.tsx derives both the header row and the colSpan it passes
 * to ImageRows from one array (`STORAGE_TABLE_COLUMNS`), so a column added or
 * removed there cannot silently desync the banner's span from the header —
 * this pins that wiring.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { STORAGE_TABLE_COLUMNS } from '../Storage';
import { ImageRows } from '../../components/images/ImageRows';

describe('the catalogue-unavailable banner spans the whole table', () => {
  it('uses the same column count as the Storage page header', () => {
    render(
      <table>
        <tbody>
          <ImageRows
            items={[{ name: 'ubuntu', origin: 'cluster', status: 'Ready' }]}
            catalogAvailable={false}
            colSpan={STORAGE_TABLE_COLUMNS.length}
          />
        </tbody>
      </table>
    );

    const banner = screen.getByRole('status').closest('td');
    expect(banner).toHaveAttribute('colspan', String(STORAGE_TABLE_COLUMNS.length));
  });
});
