# App brief — rate-limited HTTP fetcher

A small Python library that fetches many URLs concurrently while
respecting per-host rate limits. Single-file module; no CLI.

## What it should do

- Provide a callable `fetch_all(urls, *, per_host_rps=2.0,
  global_concurrency=10, timeout_s=10.0) -> list[FetchResult]`.
- `FetchResult` is a dataclass: `url: str`, `status: Optional[int]`,
  `body: Optional[bytes]`, `elapsed_s: float`, `error: Optional[str]`.
- Per-host rate limit: never more than `per_host_rps` requests/sec
  to the same hostname. Use a sliding window or token bucket — your
  call.
- Global concurrency cap: never more than `global_concurrency`
  in-flight requests total, regardless of host.
- On HTTP error (non-2xx): return a result with `status=<code>`,
  `body=<response bytes>`, `error=None`. (NOT an exception.)
- On network/timeout error: return a result with `status=None`,
  `body=None`, `error=<short description>`. (NOT an exception.)

## Constraints
- Use `httpx` (async client). Public API can be sync — wrap with
  `asyncio.run` internally so callers don't need to know.
- Order of returned results MUST match the order of `urls`.
- No retries — one attempt per URL.
- No global state. Multiple concurrent `fetch_all` calls must not
  interfere with each other's rate limits.

## Out of scope
- Caching responses.
- Cookies, auth, custom headers.
- HTTP/2.
- Streaming response bodies.
