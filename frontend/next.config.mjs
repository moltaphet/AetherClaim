/** @type {import('next').NextConfig} */
export default {
  reactStrictMode: true,
  // The app does not use next/image. Turning the optimizer off removes the
  // /_next/image endpoint, which several open Next.js 14 advisories target.
  images: { unoptimized: true },
};
