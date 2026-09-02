/**
 * Builds the template picker's <CustomSelect> options from a golden-image
 * list that may mix cluster disks with catalogue-only rows (Task 7's
 * `origin` / `catalog_ref` fields on `GoldenImage`).
 *
 * A cluster row is selected by its disk name — that is what the template
 * will reference right away. A catalogue row has no disk yet, so it is
 * selected by its `catalog_ref` instead, and the label says so: picking it
 * means accepting an import that has not happened, not an instant choice
 * like a cluster row is. Whatever consumes the resulting value should look
 * the image back up by `origin` (see `VMTemplates.tsx`'s `handleSubmit`),
 * never by guessing at the string's shape — a catalogue ref such as
 * `p/rocky-9:1` is not distinguishable from a disk name by pattern alone.
 */

export interface ImageOptionSource {
  name: string;
  display_name?: string;
  size?: string;
  origin?: 'cluster' | 'catalog';
  catalog_ref?: string | null;
}

export interface ImageOption {
  value: string;
  label: string;
}

export function imageOptions(images: ImageOptionSource[]): ImageOption[] {
  const options: ImageOption[] = [];

  for (const img of images) {
    if (img.origin === 'catalog') {
      // No catalog_ref means there is nothing to select this row by — skip
      // rather than emit an option whose value is empty/undefined.
      if (!img.catalog_ref) continue;
      options.push({
        value: img.catalog_ref,
        label: `${img.display_name || img.name} (will be imported)`,
      });
      continue;
    }

    options.push({
      value: img.name,
      label: `${img.display_name || img.name} (${img.size || 'N/A'})`,
    });
  }

  return options;
}
