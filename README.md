# ChaseMate: AI agent that chases unpaid invoices and collects through PayPal

ChaseMate reads an overdue-invoice email thread, decides tone and amount (full request, partial request or payment plan),
explains its reasoning, and after **human approval** creates a PayPal Sandbox payment request. PayPal webhooks (and the
return-URL capture) flip the dashboard from Pending to Paid. If the client stays silent, the agent escalates the tone.

## Quickstart (local)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in PayPal Sandbox keys (+ ANTHROPIC_API_KEY for the real AI)
uvicorn backend.main:app --reload
```
Open http://localhost:8000. 
## PayPal Sandbox Setup
See [docs/paypal-setup.md](docs/paypal-setup.md). Pay with a sandbox **Personal** account.

## Deploy (Render)
Push to GitHub, then Render > New > Blueprint > pick the repo (uses `render.yaml`). Add the env vars. `BASE_URL` is set automatically.

## AI Agent Flow
Thread + amount -> LLM returns JSON (tone, action, amount, reasoning, draft) -> user edits/approves -> PayPal Orders API v2 creates a
checkout link -> webhook `PAYMENT.CAPTURE.COMPLETED` / `CHECKOUT.ORDER.APPROVED` marks Paid -> "Chase again" asks the LLM for a firmer follow-up.

## Architecture
FastAPI (`backend/main.py`) + SQLite + single-page frontend (`frontend/index.html`). Endpoints: `/api/analyze`, `/api/invoices/{id}/approve|refresh|remind|simulate_paid`, `/api/webhook/paypal`, `/paypal/return`.

License: MIT
