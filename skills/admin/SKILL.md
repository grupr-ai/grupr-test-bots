# admin

OpenClaw skill — verifies that `role=admin` in the DB does NOT bleed into elevated privileges on the user-facing api.

## Run

```bash
python -m personas.admin
```

## What it covers

Logs in as an admin-role user → confirms role is reported correctly in `/api/users/me` → exercises normal user flows and verifies they look like a regular user from the api's perspective → checks that admin role doesn't grant cross-user visibility or free subscription upgrades. ~$0.50–2 per run (focused probe).

## What it does NOT cover

The CF-Access-gated admin console at `admin.grupr.ai`. That surface needs a human with a Passkey + CF Access permissions; covered by separate manual QA.
