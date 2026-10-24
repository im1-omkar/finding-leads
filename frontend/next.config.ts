import type { NextConfig } from "next";

const backendOrigin = process.env.BACKEND_API_ORIGIN?.replace(/\/+$/, "");
const apiPaths = [
  ["/auth/:path*", "/auth/:path*"],
  ["/runs", "/runs"],
  ["/runs/:path*", "/runs/:path*"],
  ["/crm/:path*", "/crm/:path*"],
  ["/mock-crm/:path*", "/mock-crm/:path*"],
  ["/config", "/config"],
  ["/health", "/health"],
  ["/docs", "/docs"],
  ["/openapi.json", "/openapi.json"],
] as const;

const nextConfig: NextConfig = {
  async rewrites() {
    if (!backendOrigin) return [];
    return apiPaths.map(([source, destination]) => ({
      source,
      destination: `${backendOrigin}${destination}`,
    }));
  },
};

export default nextConfig;
