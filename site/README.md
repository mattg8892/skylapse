# Skylapse docs site

The public site (docs, parts list, store) — built with [Astro Starlight](https://starlight.astro.build) and deployed on Cloudflare Pages from this folder.

## Edit content

Pages are Markdown in `src/content/docs/`. The sidebar order is set in `astro.config.mjs`. Images go in `src/assets/` and are referenced with relative paths (`../../assets/foo.svg`).

```bash
cd site
npm install
npm run dev       # http://localhost:4321 with live reload
npm run build     # static output in dist/
```

## Deploy (Cloudflare Pages)

One-time setup at https://dash.cloudflare.com → **Workers & Pages** → **Create** → **Pages** → **Connect to Git** → pick `mattg8892/skylapse`:

| Setting | Value |
|---|---|
| Production branch | `main` |
| Framework preset | Astro |
| Root directory | `site` |
| Build command | `npm run build` |
| Build output directory | `dist` |

Node version is pinned by `.node-version` (22). After the first deploy, **Custom domains** → add `skylapse.dev` (and `www.skylapse.dev` → redirect) — Cloudflare fills in the DNS records itself when the domain is in the same account.

Every push to `main` that touches `site/` redeploys. Preview deployments are built for other branches automatically.

## Before launch

- [ ] `SITE` in `astro.config.mjs` matches the real domain (drives canonical URLs, sitemap, OG image URL).
- [ ] Store page: replace the two disabled buttons with the real Etsy / Tindie listing URLs and remove `aria-disabled`.
- [ ] Drop real screenshots into `src/assets/` and use them on the landing page in place of / alongside the placeholder hero.
