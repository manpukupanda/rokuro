Bundled frontend assets (self-hosted, no CDN):

- Tailwind CSS 3.4.17 + Flowbite 2.5.2
  - generated bundle: `tailwind/3.4.17/flowbite.min.css`
- Shaka Player 5.1.6
  - `shaka-player.compiled.js`

Build / update policy:

1. Install frontend build dependencies from the repository root with `npm install`.
2. Rebuild the vendored stylesheet with `npm run build:css`.
3. Keep runtime-served files under this versioned `vendor/` tree and update template references when versions change.
