import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  async rewrites() {
    const base = process.env.API_BASE_URL?.replace(/\/$/, "");
    return base ? [{ source: "/bank-api/:path*", destination: `${base}/api/v1/:path*` }] : [];
  },
};

export default nextConfig;
