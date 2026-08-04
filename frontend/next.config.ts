import type { NextConfig } from "next";

const BACKEND = process.env.FIELDPILOT_API ?? "http://localhost:8100";
const EDGE = process.env.FIELDPILOT_EDGE ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      // backend REST API — same-origin proxy (no CORS needed)
      { source: "/api/:path*", destination: `${BACKEND}/:path*` },
      // edge annotated MJPEG feed
      { source: "/feed/:path*", destination: `${EDGE}/:path*` },
      // alert snapshot images (annotated bbox JPEGs the backend serves from data/alerts)
      { source: "/img/:path*", destination: `${BACKEND}/images/:path*` },
      // worker-submitted photos (question attachments, manual hazard reports)
      { source: "/uploads/:path*", destination: `${BACKEND}/uploads/:path*` },
    ];
  },
};

export default nextConfig;
