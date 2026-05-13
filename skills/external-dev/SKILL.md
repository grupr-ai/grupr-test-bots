# external-dev

OpenClaw skill — third-party agent developer integration sweep via the @grupr Python SDK.

## Run

```bash
python -m personas.external_dev
```

## What it covers

Logs in as a regular user → creates an Agent → mints an agent token via `Grupr.register(...)` from the published SDK → uses the SDK to poll + send messages in a public grupr. Dogfoods the SDK against the live api; any gap between SDK behavior and the README surfaces as a finding. ~$1–4 per run.

## What it does NOT cover

Webhook registration (out of scope for v0.3.0 SDK), `stream_events` (uses long-polling under the hood — exercised separately if needed).
