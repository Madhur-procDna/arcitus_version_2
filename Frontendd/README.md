This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Takeda SDA backend

The chat UI calls the Python QA API (`POST /query`):

1. Start the API from **`Backend/src`** (see `Backend/README.md`). The port must match **`SDA_BACKEND_URL`** in **`.env.local`**:

   `uvicorn api_server:app --reload --host 127.0.0.1 --port 8000`

   (`Backend/run_api.ps1` uses **8001** — if you use that script, set **`SDA_BACKEND_URL=http://127.0.0.1:8001`**.)

2. Copy **`.env.local.example`** → **`.env.local`**. In dev, chat uses **`/api/query`** (Next.js → Python). To call FastAPI directly from the browser instead, set **`NEXT_PUBLIC_SERVER_URL`** (CORS must allow `localhost:3000`).

3. Run `npm run dev` and open [http://localhost:3000](http://localhost:3000).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.
