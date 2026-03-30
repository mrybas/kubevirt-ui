/**
 * Design tokens — single source of truth for typography, spacing, layout, colors.
 * Use these in components instead of raw Tailwind values.
 */

export const typography = {
  h1: 'text-2xl font-bold',       // 24px / 700
  h2: 'text-xl font-semibold',    // 20px / 600
  body: 'text-sm',                // 14px / 400
  small: 'text-xs',               // 12px
} as const;

export const spacing = {
  // 8px grid
  1: '0.5rem',   // 8px
  2: '1rem',     // 16px
  3: '1.5rem',   // 24px
  4: '2rem',     // 32px
  6: '3rem',     // 48px
} as const;

export const layout = {
  sidebarWidth: 240,         // px, w-60
  sidebarCollapsed: 64,      // px, w-16
  buttonHeight: 36,          // px, h-9
  rowHeight: 40,             // px, h-10
} as const;

export const colors = {
  primary: 'sky-600',
  bg: 'zinc-950',
  surface: 'zinc-800',
  text: 'zinc-100',
  muted: 'zinc-400',
} as const;
