import type { NextConfig } from "next";

// Additional CSP connect-src origins (e.g. production WebSocket endpoints).
// Set NEXT_PUBLIC_CSP_CONNECT_EXTRA in .env.local for AWS deployment.
const cspConnectExtra = process.env.NEXT_PUBLIC_CSP_CONNECT_EXTRA || "";

const nextConfig: NextConfig = {
  output: 'standalone',
  async headers() {
    const connectSrcParts = [
      "'self'",
      // Local dev WebSocket (HMR) — harmless on production, essential locally
      "ws://localhost:*",
      "wss://localhost:*",
      "ws://127.0.0.1:*",
      "wss://127.0.0.1:*",
    ];
    if (cspConnectExtra.trim()) {
      connectSrcParts.push(...cspConnectExtra.trim().split(/\s+/));
    }

    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          {
            key: "Content-Security-Policy",
            value: [
              "default-src 'self'",
              // Next.js 15 requires 'unsafe-inline' for hydration scripts + style chunks
              "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
              "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
              "font-src 'self' https://fonts.gstatic.com data:",
              "img-src 'self' data: blob:",
              // Allow same-origin fetch + WebSocket for HMR in dev + extra origins for production
              `connect-src ${connectSrcParts.join(" ")}`,
              "frame-ancestors 'none'",
              "base-uri 'self'",
              "form-action 'self'",
            ].join("; "),
          },
        ],
      },
    ];
  },
};

export default nextConfig;

