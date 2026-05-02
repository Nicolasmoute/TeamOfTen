# TeamOfTen brand mark — drop-in pack

The orange "10" disk, ready to use as favicon, app icon, lockup mark.
Hand it (and these instructions) to Claude Code.

## Files in this folder

| File | Purpose |
| --- | --- |
| `teamoften-mark.svg` | 64-unit master. Use anywhere — favicon, header, OG card source. |
| `teamoften-favicon-16.svg` | Hand-tuned 16×16 with raw `1`/`0` paths (no text rendering). Sharper than the master at the address-bar size. |
| `index-head-snippet.html` | The exact `<head>` tags to paste into the app shell. |

## Tell Claude Code

> Add the TeamOfTen brand mark as the favicon and tab title.
>
> 1. Copy `brand/teamoften-mark.svg` and `brand/teamoften-favicon-16.svg` into `server/static/`.
> 2. Generate a 180×180 PNG of the mark (`teamoften-mark-180.png`) for iOS home-screen and put it next to them. Easiest path: `rsvg-convert -w 180 -h 180 brand/teamoften-mark.svg -o server/static/teamoften-mark-180.png` (or any SVG-to-PNG tool — ImageMagick, Inkscape, online).
> 3. In `server/templates/index.html` (or wherever the SPA `<head>` lives), inside `<head>`, add the contents of `brand/index-head-snippet.html`. If a `<title>` or `rel="icon"` already exists, replace it.
> 4. If the static mount path isn't `/`, prefix the `href`s in the snippet (e.g. `/static/teamoften-mark.svg`).

## The colour

Stadium-night accent: `oklch(0.78 0.19 55)` ≈ `#F5832E`. Matches the Marketing canvas. Keep this hex as the single source of truth for the brand orange across README, OG cards, and UI accents.

## The geometry

- Disk: filled circle, 4-unit safe margin from the viewBox edge.
- Type: Inter Tight 900, optical size 34/64 ≈ 53% of the disk diameter, `letter-spacing: -0.025em` so the "10" reads as a tight unit, not two glyphs.
- Ink colour `#0F1014` (near-black, not pure) so it matches `--bg` from `styles/tokens.css` instead of fighting it.
