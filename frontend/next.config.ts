import type { NextConfig } from "next";
import path from "node:path";
const apiBase = process.env.API_BASE_URL || "http://127.0.0.1:8000";
const nextConfig: NextConfig = { outputFileTracingRoot: path.resolve(__dirname), async rewrites() { return [{ source: "/api/:path*", destination: `${apiBase}/api/:path*` }]; } };
export default nextConfig;
