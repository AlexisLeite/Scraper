/** The Python service serves the exported portal and its existing /api endpoints. */
export default {
  output: process.env.NODE_ENV === 'development' ? undefined : 'export',
  trailingSlash: true,
  images: { unoptimized: true },
  ...(process.env.NODE_ENV === 'development' ? {
    async rewrites() {
      return [{ source: '/api/:path*', destination: `${process.env.BACKEND_URL || 'http://127.0.0.1:8787'}/api/:path*` }];
    },
  } : {}),
};
