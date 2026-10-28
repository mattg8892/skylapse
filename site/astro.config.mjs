// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// Change this when the domain is bought — it drives canonical URLs, sitemap and OG tags.
const SITE = 'https://skylapse.dev';

export default defineConfig({
  site: SITE,
  // The site used to have thirteen pages; it now has six. Old links keep working.
  redirects: {
    '/getting-started': '/build',
    '/hardware': '/parts',
    '/cameras': '/parts/#camera-and-lens',
    '/dew-heater': '/build/#7-switch-the-heater-on',
    '/network': '/using/#network',
    '/phone-alerts': '/using/#phone-alerts',
    '/storage-and-raw': '/using/#storage--raw',
    '/troubleshooting': '/help',
    '/status': '/help/#what-is-verified-on-hardware',
    '/design': '/help/#for-developers',
    '/development': '/help/#for-developers',
  },
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
        { label: 'Home', link: '/' },
        { label: 'Parts list', slug: 'parts' },
        { label: 'Build', slug: 'build' },
        { label: 'Using the app', slug: 'using' },
        { label: 'Help & status', slug: 'help' },
        { label: 'Store', slug: 'store' },
        { label: 'Gallery', slug: 'gallery' },
      ],
      head: [
        { tag: 'meta', attrs: { property: 'og:image', content: `${SITE}/og.png` } },
        { tag: 'meta', attrs: { name: 'twitter:card', content: 'summary_large_image' } },
        { tag: 'link', attrs: { rel: 'apple-touch-icon', href: '/apple-touch-icon.png' } },
      ],
    }),
  ],
});
