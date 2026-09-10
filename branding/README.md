# Branding assets

Source and exports for Calendar Bridge's icon, used for the submission to
[home-assistant/brands](https://github.com/home-assistant/brands)
(`custom_integrations/calendar_bridge/`) — that repository is what actually
supplies the icon shown in the Home Assistant UI and in HACS; nothing here
is read directly by the integration itself.

- `icon.svg` — editable source. Flat design, transparent background,
  256x256 viewBox.
- `icon.png` (256x256) / `icon@2x.png` (512x512) — rasterized exports,
  matching what `home-assistant/brands` expects for `icon.png`/`icon@2x.png`.

To regenerate the PNGs after editing the SVG, re-render at 512x512 with
anti-aliasing (e.g. via a browser or any SVG rasterizer) and downscale the
256x256 version from that, rather than rendering 256x256 directly, to avoid
jagged edges.
