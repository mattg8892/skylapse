// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// Change this when the domain is bought — it drives canonical URLs, sitemap and OG tags.
const SITE = 'https://skylapse.dev';

export default defineConfig({
  site: SITE,
  integrations: [
    starlight({
      title: 'Skylapse',
      description:
        'Open-source night-sky timelapse software for Raspberry Pi. Point a camera at the sky, run it from your phone.',
      logo: {
        light: './src/assets/logo-light.png',
        dark: './src/assets/logo-dark.png',
        replacesTitle: true,
        alt: 'Skylapse',
      },
      favicon: '/favicon.png',
      social: [
        { icon: 'github', label: 'GitHub', href: 'https://github.com/mattg8892/skylapse' },
      ],
      editLink: {
        baseUrl: 'https://github.com/mattg8892/skylapse/edit/main/site/',
      },
      customCss: ['./src/styles/custom.css'],
      lastUpdated: true,
      sidebar: [
        {
          label: 'Start here',
          items: [
            { label: 'Getting started', slug: 'getting-started' },
            { label: 'Hardware & parts list', slug: 'hardware' },
            { label: 'Cameras & lenses', slug: 'cameras' },
          ],
        },
        {
          label: 'Using Skylapse',
          items: [
            { label: 'The screens', slug: 'using' },
            { label: 'Storage & RAW', slug: 'storage-and-raw' },
            { label: 'Wi-Fi & access point', slug: 'network' },
            { label: 'Phone alerts', slug: 'phone-alerts' },
            { label: 'Dew heater', slug: 'dew-heater' },
          ],
        },
        {
          label: 'Help',
          items: [
            { label: 'Troubleshooting', slug: 'troubleshooting' },
            { label: 'Status & roadmap', slug: 'status' },
          ],
        },
        {
          label: 'Project',
          items: [
            { label: 'Development', slug: 'development' },
            { label: 'Design notes', slug: 'design' },
            { label: 'Store', slug: 'store' },
          ],
        },
      ],
      head: [
        { tag: 'meta', attrs: { property: 'og:image', content: `${SITE}/og.png` } },
        { tag: 'meta', attrs: { name: 'twitter:card', content: 'summary_large_image' } },
        { tag: 'link', attrs: { rel: 'apple-touch-icon', href: '/apple-touch-icon.png' } },
      ],
    }),
  ],
});
