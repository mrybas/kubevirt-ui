import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ImageRows } from '../components/images/ImageRows';

function rows(ui: React.ReactNode) {
  // ImageRows renders <tr>s, which are only valid inside a table.
  return render(<table><tbody>{ui}</tbody></table>);
}

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

  // The Scope column, which the Images tab lost when these rows moved out of
  // Storage.tsx. `scope` was still declared on ImageRowItem and simply never
  // read, so the badge disappeared from one tab and stayed on the Data Disks
  // tab next to it — with the Harbor flag OFF, i.e. for every deployment.
  it('shows a project-scoped disk as available to all environments', () => {
    rows(
      <ImageRows
        items={[
          {
            name: 'ubuntu',
            origin: 'cluster',
            status: 'Ready',
            scope: 'project',
            environment: 'dev',
          },
        ]}
        catalogAvailable
      />
    );

    expect(screen.getByText(/all envs/i)).toBeInTheDocument();
    // The environment name must NOT also be shown: "dev" and "all envs" are
    // contradictory answers to the same question.
    expect(screen.queryByText('dev')).not.toBeInTheDocument();
  });

  it('shows the environment name for an environment-scoped disk', () => {
    rows(
      <ImageRows
        items={[
          {
            name: 'ubuntu',
            origin: 'cluster',
            status: 'Ready',
            scope: 'environment',
            environment: 'dev',
          },
        ]}
        catalogAvailable
      />
    );

    expect(screen.getByText('dev')).toBeInTheDocument();
    expect(screen.queryByText(/all envs/i)).not.toBeInTheDocument();
  });

  it('says Catalog for a catalogue row, which is in no environment at all', () => {
    rows(
      <ImageRows
        items={[{ name: 'rocky-9:1', origin: 'catalog', status: 'Catalog', scope: 'project' }]}
        catalogAvailable
      />
    );

    expect(screen.getByText('Catalog')).toBeInTheDocument();
    expect(screen.queryByText(/all envs/i)).not.toBeInTheDocument();
  });
});
