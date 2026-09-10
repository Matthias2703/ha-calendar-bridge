# Branding assets

Source for Calendar Bridge's icon. Since Home Assistant 2026.3.0, custom
integrations serve their own brand images directly from a `brand/` folder
inside the integration (see the
[brands proxy API announcement](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api));
submitting to [home-assistant/brands](https://github.com/home-assistant/brands)
is no longer accepted for custom integrations. The actual served files live
at
[`custom_components/calendar_bridge/brand/`](../custom_components/calendar_bridge/brand/)
(`icon.png`, `icon@2x.png`, `logo.png`) — this folder only holds the editable
source and is not read by the integration itself.

- `icon.svg` — editable source. Flat design, transparent background,
  1254x1254 viewBox.

To regenerate the PNGs after editing the SVG:

```
npx --yes @resvg/resvg-js  # or any SVG rasterizer with alpha support
```

(a small one-off Node script using `@resvg/resvg-js`'s `Resvg` API works
well — render at width 512 for `icon@2x.png` and width 256 for `icon.png`,
with `background: 'rgba(0,0,0,0)'`), then copy both into
`custom_components/calendar_bridge/brand/` as `icon.png`/`icon@2x.png` (and
`logo.png`, currently identical to `icon.png` since there's no separate
wordmark).
