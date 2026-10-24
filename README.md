# Lead Research & CRM Loader

See the implementation guide at [lead-research-crm-loader/README.md](lead-research-crm-loader/README.md).

To start the complete application from this directory:

```bash
test -f .env || cp .env.example .env
docker compose up --build
```

The UI is at <http://localhost:3000>; API docs are at <http://localhost:8000/docs>. Set `NUMBER_OF_LEADS` in the root `.env` to control the authoritative target (1–200). Compose runs the local API, PostgreSQL, worker, and frontend; local mock research remains enabled by default.

## Free personal demo deployment

The included deployment configuration uses Vercel for the Next.js frontend, Render for the FastAPI API, and Neon for PostgreSQL. It deploys no worker and sets `RESEARCH_ENABLED=false`, so sign-in, saved history, and CRM reads stay available while new research and approval requests are rejected before a job is queued. Existing queued runs remain saved and idle.

This setup is for a personal, non-commercial demo. Vercel Hobby is for non-commercial use. Render's free web services are not intended for production applications and sleep after 15 minutes without traffic; the first request after idle can take about a minute to wake. Render Free also blocks SMTP ports 25, 465, and 587. See [Vercel Hobby](https://vercel.com/docs/plans/hobby) and [Render's free service limits](https://render.com/docs/free).

1. Push the repository to a Git provider. Create a Neon PostgreSQL database and copy its connection string (including the provider's required SSL options).
2. Create a Vercel project from the repository with Root Directory set to `frontend`, then deploy once to get its HTTPS URL. API calls will not work until the backend proxy is configured in step 4.
3. In Render, create a Blueprint from the repository using [`render.yaml`](render.yaml). Supply `DATABASE_URL`, `FRONTEND_PUBLIC_URL`, and `FRONTEND_ORIGINS` using the Vercel HTTPS origin. The blueprint creates one free Docker web service only; it does not create a database or worker. The Render-generated `APP_SESSION_SECRET` is used for secure production sessions.
4. Add `BACKEND_API_ORIGIN` in Vercel with the Render service's HTTPS origin, for example `https://lead-research-api.onrender.com` (no trailing slash), then redeploy the frontend. Do not set `NEXT_PUBLIC_API_BASE_URL` on Vercel; browser API calls use same-origin rewrites so auth cookies and CSRF checks share the frontend domain. The rewrites are configured in [`frontend/next.config.ts`](frontend/next.config.ts). If you later add a custom frontend domain, update Render's `FRONTEND_PUBLIC_URL` and include the exact HTTPS origin in `FRONTEND_ORIGINS`.
5. For password-reset and verification email, configure Render's `SMTP_HOST`, `SMTP_FROM`, `SMTP_USERNAME`, and `SMTP_PASSWORD` with a mail relay that supports STARTTLS on a Render-allowed port (such as 2525, if supported by the provider). Without SMTP, production auth email links cannot be delivered.
6. Keep `APP_ENV=production`, `RESEARCH_ENABLED=false`, and `MOCK_MODE=true` on Render. Do not add a worker service. Configure deployment secrets in provider dashboards; use root [`.env.example`](.env.example) only as a template for local setup. Anyone cloning the app can copy it to `.env`, edit values, and run the local Compose workflow.

For a durable public production service, move off these free demo tiers and enable research only after deploying a worker and reviewing operating costs.
