# Archetype AI Design System

Compose UIs from the design system's npm packages — never hand-roll components,
tokens, fonts, or brand styling the system already provides.

## Stack

- Svelte 5 with runes (`$props`, `$state`, `$derived`, `$bindable`)
- Tailwind v4 with design tokens from `@archetypeai/ds-lib-tokens` (source of truth)
- `@archetypeai/ds-ui-svelte-console` — production-ready primitives, stable API
- `@archetypeai/ds-ui-svelte-labs` — experimental primitives built on console; may change between 0.x versions
- bits-ui underneath for headless behavior; icons from `@lucide/svelte`

## The manifest is the catalog

**`ds-manifest.json` (project root) lists every component**: import path, what
it's for, variant axes with defaults, and `usage` — idiomatic prop combinations
observed in the real product. Read a component's entry before using it instead
of guessing names or props.

```svelte
<script lang="ts">
  import { Button } from '@archetypeai/ds-ui-svelte-console/primitives/button';
  import * as Card from '@archetypeai/ds-ui-svelte-console/primitives/card';
  import SensorChart from '@archetypeai/ds-ui-svelte-labs/primitives/sensor-chart';
</script>
```

## Modifying a component

Import from the packages by default. Only when a component must diverge, pull
its editable source from the registry (the component's `source` URL in the
manifest):

```bash
npx shadcn-svelte@latest add <component source URL>
```

The source lands under `$lib/components/ui/<name>/` — import the modified copy
from there and keep everything else on package imports. Never edit files in
`node_modules`.

## CSS wiring (order-critical)

```css
@import 'tailwindcss';
@import 'tw-animate-css';
@import '@archetypeai/ds-lib-tokens/fonts.css';
@import '@archetypeai/ds-lib-tokens/theme.css';
@source '../../node_modules/@archetypeai/ds-ui-svelte-console/dist';
@source '../../node_modules/@archetypeai/ds-ui-svelte-labs/dist';
```

The `@source` directives make Tailwind emit classes used inside the package
dists — removing them silently unstyles the components.

## Tokens

Use semantic tokens for anything themed; standard Tailwind for layout.

- Surfaces/text: `bg-background`, `text-foreground`, `bg-card`, `bg-muted`,
  `text-muted-foreground`, `border-border`, `bg-accent`, `bg-primary`,
  `text-primary-foreground`, `bg-destructive`
- Status: `text-atai-good`, `text-atai-warning`, `text-atai-critical`,
  `text-atai-neutral` (status badges take only the text colour; the outline follows it)
- Brand: `bg-atai-brand-babyblue`, `bg-atai-brand-yellow`,
  `text-atai-always-black`, `text-atai-always-white`
- Icon strokes on lucide icons: `stroke-icon-default`,
  `stroke-icon-interactive`, `stroke-icon-status`, `stroke-icon-emphasis`
- Spacing scale: `xs sm md lg xl` work as `p-lg`, `gap-md`, `space-y-xl`, `mb-sm`
- Chart series colors: `var(--chart-1)` … `var(--chart-5)`
- Radius on interactive elements: `rounded-interactive`
- Scrollbars are owned by the theme (`--scrollbar`, `--scrollbar-track`,
  `--scrollbar-width`). Every `overflow-*` container is already on-brand in both
  themes — write no scrollbar CSS at all. `bg-scrollbar` exists for a
  custom-rendered thumb

Standard Tailwind is fine for spacing/sizing (`p-4`, `w-full`, `gap-2`) and
layout (`flex`, `grid`, `absolute`). Status colors communicate state — never
use them decoratively.

## Typography

The theme pairs every `text-*` step with its own line-height and letter-spacing
(`--text-sm--line-height`, `--text-sm--letter-spacing`, …), so `text-sm` alone
is on-brand. Never add `leading-*` or `tracking-*` next to a `text-*` utility —
the ramp already supplies both. `text-2xs` (10px) is the floor for dense
metadata; never use arbitrary sizes like `text-[11px]`.

The theme's base layer styles only authored-content elements: `body` (14px
sans), `h1`–`h6` (30px → 14px, `h4`–`h6` muted, `h1` title-cased), `p`,
`small`, `strong`, `blockquote`, plus `cursor-pointer` on `a`. It deliberately
does **not** style `span`, `label`, `em`, `code` or `pre` — and gives `a` no
typography either — they inherit from their parent, so set the size once on the
parent instead of on every inline child (`code`/`pre` get the mono family from
Tailwind preflight and stay the same size as their text). Bare links are
visually flat until you style them, but every `a` shows the pointer cursor even
without an `href`; use the `Label` primitive for labels.
`h1` is `capitalize` by default (use `normal-case` for ids or sentence-case
copy); uppercase belongs to `Label` variants and `typography="mono"`, never to
a heading.

Default to sans for all text. `font-mono` is reserved for exactly two cases:

- Primitives that already use it in the design system source of truth (their
  built-in styles or `typography="mono"` variants) — don't add mono to others
- Values that would cause layout shift as they update, and ids — numbers,
  timestamps, session/job ids

Everything else — body copy, descriptions, headings, buttons without a mono
variant — stays sans. Never apply mono decoratively.

## API keys and endpoints

When a demo needs an API key, never read it from `.env` — add an `Input` to
the `Menubar` (children slot) so the user pastes the key at runtime; hold it
in `$state` and pass it to API calls. Endpoints/base URLs do come from `.env`.

## Dark mode

`.dark` class on `<html>` switches every token. Use the controller from the
console package — don't write your own:

```svelte
import { darkMode } from '@archetypeai/ds-ui-svelte-console/primitives/theme';

darkMode.set(window.matchMedia('(prefers-color-scheme: dark)').matches); // init
darkMode.toggle(); // switch
darkMode.current; // reactive read
```

`CodeBlock` needs nothing here: it highlights with both themes at once (shiki
emits the dark colours as `--shiki-dark` custom props) and flips them in CSS, so
theme switches need no `{#key}` wrapper and no re-highlight.

## Page shell

Dashboards are full-viewport: branded `Menubar` (labs) on top with action
buttons as children, content in a padded `main`:

```svelte
<div class="bg-background flex h-screen flex-col">
  <Menubar>
    <Button variant="link" class="text-muted-foreground no-underline hover:underline">Action</Button>
  </Menubar>
  <main class="min-h-0 flex-1 p-lg">…panels…</main>
</div>
```

Mount `<Toaster />` (sonner) once at the root; fire `toast.success(...)` /
`toast.error(...)` anywhere.

## Charts

- Time series → `SensorChart`; embeddings/clusters → `ScatterChart` (see their
  `usage` in the manifest, incl. streaming via `maxPoints`)
- Custom charts: wrap layerchart in `Chart.Container` with a
  `config` mapping data keys to `{ label, color: 'var(--chart-N)' }`
- `layerchart` is pinned to an exact prerelease — never loosen or bump it

## Custom components

Compose project-specific components from package primitives in
`$lib/components/ui/custom/`. Follow the package conventions:

```svelte
<script lang="ts">
  import { cn } from '$lib/utils.js';
  let { class: className, ref = $bindable(null), children, ...restProps } = $props();
</script>

<div bind:this={ref} class={cn('bg-card p-lg', className)} {...restProps}>
  {@render children?.()}
</div>
```

- `cn()` comes from `$lib/utils.js` (re-export of the console helper) — never
  create a local clsx/tailwind-merge copy, never concatenate class strings
- Keep API/streaming logic (fetch, SSE, polling) in `src/lib/api/*`, not in
  components

## Accessibility

- `aria-label` on every icon-only control (buttons, switches, sliders, toggles)
- `aria-hidden="true"` on decorative icons; `sr-only` for visually hidden text
- Don't remove focus rings; components ship correct focus states

## Hard rules

- Never edit `node_modules`; never copy package source by hand — eject via the
  registry instead
- Variant values named after console-product screens (`runSession`, `topNav*`,
  `sideBarSelection`, `lensTray*`, `backToDashboard`) exist in the package
  types but are internal to the Archetype AI console product — never use them
- Keep the `@source` directives and the CSS import order exactly as above
- Never add global element rules (`span`, `a`, `label`, `p`, `h1`…) in app CSS,
  and never pair `leading-*`/`tracking-*` with a `text-*` utility — the theme's
  type scale and base layer own both; size a parent, not its inline children
- Never style scrollbars in app code — no `::-webkit-scrollbar` rules, no
  `scrollbar-color`/`scrollbar-width`. The theme sets both, and setting
  `scrollbar-*` locally also silently disables the webkit pseudo-elements on
  that element
- `layerchart` stays pinned exactly
