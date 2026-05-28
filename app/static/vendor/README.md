Bundled frontend assets (self-hosted, no CDN):

- Bootstrap 5.3.8
  - `bootstrap.min.css`
  - `bootstrap.bundle.min.js`
- Shaka Player 5.1.6
  - `shaka-player.compiled.js`

Update policy:

1. Fetch latest stable assets from official package releases.
2. Place files under versioned directories in this `vendor/` tree.
3. Update template references to the new versioned paths.
