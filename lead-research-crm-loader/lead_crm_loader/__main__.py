from __future__ import annotations

import json
import os
import sys

import uvicorn


def main() -> None:
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    print(f"Starting Lead Research & CRM Loader API on http://{host}:{port}")
    print(f"Open http://{host}:{port}/docs for the interactive API.")
    # Keep the demo bound to localhost unless configured via HOST env var. Add authentication before exposing this service publicly.
    uvicorn.run("lead_crm_loader.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
