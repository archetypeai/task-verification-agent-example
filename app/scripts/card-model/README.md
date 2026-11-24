# Console card model

The animated isometric drawing of the manifold cap SOP that the Archetype console shows on this example's
"See it in action" card (`/console/blueprints/<key>`, agent `tva`). The console keeps only the
generated SVG, as `services/console_2_service/service_frontend/src/lib/components/agents/models/tva-cap.svg`
in `atai_core`.

```sh
cd app && node scripts/card-model/tva-cap.mjs --out tva-cap.svg                       # the SVG
cd app && node scripts/card-model/tva-cap.mjs --out tva-cap.svg --preview preview.html  # plus a standalone page; ?t=<s> pauses it at that second
```

- `tva-cap.mjs` draws the model and scripts its motion; `common.mjs` holds what every card model
  shares (the SMIL, the console's token classes, writing the files).
- The SVG is styled with the console's design-token utilities only (`fill-card`,
  `stroke-foreground/50`, `dark:` variants), so it themes with the console.
- The motion is SMIL inside the SVG, so the console runs it without a script; it only pauses the
  SVG offscreen and under `prefers-reduced-motion`, where time 0 shows the model at rest.
- `kit/` is the vendored Anatomy kit (MIT, see `kit/LICENSE`), unchanged.

To update the card, regenerate the SVG here, copy it over the console's file and open a PR in
`atai_core`.
