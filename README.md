# Lead Research & CRM Loader

See the implementation guide at [lead-research-crm-loader/README.md](lead-research-crm-loader/README.md).

To start the complete application from this directory:

```bash
test -f lead-research-crm-loader/.env || cp lead-research-crm-loader/.env.example lead-research-crm-loader/.env
docker compose up --build
```

The UI is at <http://localhost:3000>; API docs are at <http://localhost:8000/docs>. Set `NUMBER_OF_LEADS` in `lead-research-crm-loader/.env` to control the authoritative target (1–200).
