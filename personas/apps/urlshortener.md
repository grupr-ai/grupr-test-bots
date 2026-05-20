# App brief — URL shortener CLI

A small Python CLI for shortening URLs locally. Single-file module
+ a small `__main__` block. Persistent store backed by SQLite so
codes survive restarts.

## What it should do

- `add <url>` → mint a unique short code (6 chars, [a-zA-Z0-9]),
  store the mapping, print the code.
- `resolve <code>` → print the original URL, or exit non-zero
  with an error if the code doesn't exist.
- `list` → print every (code, url, created_at) row, newest first.
- `delete <code>` → remove the mapping; exit non-zero if missing.

## Constraints
- Standard library only. No Flask, no FastAPI — just `argparse`,
  `sqlite3`, `secrets`, `string`, etc.
- DB path defaults to `~/.urlshortener.db`. Override with `--db`
  on every command.
- Code generation: random from the alphabet, retry on collision
  with a hard cap (~5 attempts) before raising.
- URL validation is **best-effort** — reject if it lacks a scheme
  (`http://` or `https://`), otherwise accept anything.

## Out of scope
- HTTP server / web UI.
- Click counts / analytics.
- Auth.
- Cleanup / TTL.
