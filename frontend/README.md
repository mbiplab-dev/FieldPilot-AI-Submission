This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## FieldPilot site-manager dashboard

Pages: `/` overview, `/live` edge stream + event ticker, `/alerts` (with supervisor
approve/reject), `/rfis` review queue, `/workers`, `/zones`, `/rules`, `/learning`
(fine-tune + mAP50 gate), `/blueprints` (RAG index + search), `/activity`.

Backend wiring (see `next.config.ts`):

| Path     | Proxied to                        |
| -------- | --------------------------------- |
| `/api/*` | backend REST API on `:8100`       |
| `/feed/*`| edge MJPEG stream on `:8000`      |
| `/img/*` | backend `/images/*` (alert JPEGs) |

Live push uses a websocket **directly** against the backend (`ws://<host>:8100/ws`),
because Next.js rewrites do not proxy websockets. Override the base URL when the
backend is not on the same host:

```bash
# frontend/.env.local
NEXT_PUBLIC_FIELDPILOT_WS="ws://backend.internal:8100/ws"
```

Every page keeps polling as a fallback, so a closed socket degrades the dashboard
instead of freezing it — look for the `degraded · polling` chip in the header.

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
