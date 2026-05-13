# conversational

OpenClaw skill — single in-character contribution to a multi-bot Grupr Workshop.

## Run

```bash
python -m personas.conversational \
    --role skeptical_engineer \
    --grupr-id <uuid> \
    --email-env GRUPR_TEST_CONV_A_EMAIL
```

The orchestration lives in `../../scripts/multi-bot-workshop.sh`, which sets up the grupr and rotates three bots in turn.

## Roles

- `skeptical_engineer` — pokes holes, asks scaling/edge-case questions
- `enthusiastic_pm` — pushes toward shipping a minimum viable version
- `cautious_security` — surfaces risk + proposes mitigations

## What it covers

Each invocation: log in → read recent thread → compose ONE in-character message → post → exit. The cumulative scenario validates that three concurrent agents can read/write to the same grupr in deterministic order, references previous contributions correctly (the "AIs see each other" thesis), and produces a coherent thread. ~$0.10 per invocation.
