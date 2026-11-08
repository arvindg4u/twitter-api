# MCP Reliability Profile

Operational notes for the `twitter-api` MCP server: which tools are safe to
retry, which failures are deterministic, and which limits come from X rather
than from this service.

- **Last verified:** 2026-11-08 against deploy `c75a3a6`
- **Endpoint:** `https://twitter-api-gjyc.onrender.com/mcp/` (Streamable HTTP)
- **Transport:** stateless, `json_response=True` — every tool call is independent;
  there is no session to recover.

---

## TL;DR

1. **Retry reads once.** Transient X/Render failures usually clear on one retry.
2. **Never blindly retry writes.** `post_tweet`, `send_dm`, `post_tweet_with_media`
   are **not idempotent** — a timeout may mean the write succeeded.
3. **Don't retry content errors.** Bad URL, non-image, oversized media, stale
   tweet ID, invalid poll options: these will fail identically every time.
4. **`get_notifications` works but should stay optional.** Treat an empty result
   or an unexpected shape as informational, not as a hard dependency.
5. **`count` is enforced server-side now.** The service trims full pages that X
   returns, so callers get at most `count` items.

---

## How errors surface

Tools raise `ToolError` for anticipated failures, so the client receives:

```
is_error = true
Error executing tool <name>: <actionable reason>
```

Anything else is a crash and shows only `Error executing tool <name>`; the
traceback stays in Render logs. If you see the bare form, check logs before
retrying.

### Error signatures → meaning → action

| Signature | Meaning | Action |
|---|---|---|
| `Socket timeout ... onrender.com` | Client-side timeout, usually Render cold start or concurrency | Retry once after a short delay |
| `429`, `TooManyRequests` | X rate limit | Back off; do not hammer |
| `500`, `502`, `503`, `504` | Upstream/edge transient | Retry with backoff |
| `Timeout`, `Connect`, `ReadError`, `ConnectionReset`, `SSL` | Transport transient | Retry with backoff |
| `code 344` / `daily limit` | X daily tweet/message limit | Wait for reset; do not retry |
| `code 279` | DM conversation does not exist / recipient does not accept DMs | Do not retry |
| `code 476` / `not verified` | Sender is not verified to send message requests | Do not retry; verify the account |
| `code 349` | Recipient does not accept messages from you | Do not retry |
| `403` on a DM | Recipient privacy or account lacks DM permission | Do not retry |
| `HTTP 400` from `upload.wikimedia.org` | Invalid Wikimedia thumbnail size (X not involved) | Use the original file URL or an allowed size |
| `URL did not return an image` | Download succeeded but body was HTML/other | Fix the URL |
| `X's limit is ... bytes` | Image >5 MB or GIF >15 MB | Resize/re-encode |
| `status: 404` on followers GraphQL | X rotated the query ID | Automatic v1.1 REST fallback (no action) |

---

## Tool matrix

`Retry` = is it safe to call the same tool again with the same arguments?

### Read tools

| Tool | Status | Retry | Notes |
|---|---|---|---|
| `search_tweets` | Stable | ✅ 1x | Supports `Top`/`Latest`/`Media`; authed first, guest fallback |
| `search_users` | Stable | ✅ 1x | Keyword search or exact `@handle` resolve |
| `get_user` | Stable | ✅ 1x | Guest path, falls back to authed session |
| `get_user_tweets` | Stable | ✅ 1x | `count` enforced; `ranked` and `chronological` |
| `get_tweet` | Stable | ✅ 1x | Falls back to FxTwitter embed API |
| `get_tweet_replies` | Stable | ✅ 1x | Conversation context |
| `get_trends` | Stable | ✅ 1x | `tweet_count` is always `null` upstream; use `rank` |
| `get_followers` | Stable | ✅ 1x | GraphQL 404 → v1.1 `followers/list.json`; `count` enforced |
| `get_following` | Stable | ✅ 1x | Same fallback; `count` enforced |
| `get_user_likes` | Stable | ✅ 1x | Empty result is valid when the user hides likes (an empty list can render as an empty string) |
| `get_home_timeline` | Stable | ✅ 1x | `count` enforced |
| `get_bookmarks` | Stable | ✅ 1x | `count` enforced |
| `get_mentions` | Stable | ✅ 1x | Empty list when there are no mentions |
| `get_notifications` | Works | ✅ 1x | Returns `{id, kind, message, from_user{id,screen_name,name}, tweet_id, timestamp_ms}`; `raw=true` returns X's unparsed objects. Recommendations (`kind=recommendation_icon`) carry a `from_user.id` but no inlined profile |

### Write tools

| Tool | Status | Retry | Notes |
|---|---|---|---|
| `post_tweet` | Works | ⚠️ only if confirmed not created | Code 344 daily limit is surfaced; not idempotent |
| `reply_to_tweet` | Works | ⚠️ same as post | Not idempotent |
| `quote_tweet` | Works | ⚠️ same as post | Not idempotent |
| `create_poll_tweet` | Works | ⚠️ same as post | Needs 2–4 non-empty options |
| `post_tweet_with_media` | Works | ⚠️ upload is safe, post is not | Uploads 1–4 images, then creates the tweet |
| `upload_media` | Stable | ✅ retry on network/5xx | Deterministic failures: bad URL, non-image, >5 MB, >15 MB GIF |
| `delete_tweet` | Stable | ⚠️ | Deleting twice may return 404; treat 404 as success |
| `like_tweet` / `unlike_tweet` | Stable | ✅ | Idempotent |
| `retweet` / `unretweet` | Stable | ✅ | Idempotent |
| `follow_user` / `unfollow_user` | Stable | ✅ | Idempotent: checks `friendships/show` first and returns `already_following` / `already_unfollowed` instead of failing on a duplicate |
| `bookmark_tweet` / `unbookmark_tweet` | Stable | ✅ | Idempotent |
| `send_dm` | Account-dependent | ❌ never auto-retry | Not idempotent; a timeout may mean the DM was sent. Code 476 = sender not verified for message requests; 349 = recipient does not accept messages from you; 403 = recipient/privacy; 344 = daily limit. The tool itself works |

---

## Known X-side behaviors and limits

### Media (`upload_media`, `post_tweet_with_media`)

- **Image limit:** 5 MB (`tweet_image`). **GIF limit:** 15 MB (`tweet_gif`).
- The service downloads the URL first, then uploads the bytes with an explicit
  `media_category` (`tweet_image` / `tweet_gif`).
- **Wikimedia thumbnails:** `upload.wikimedia.org/.../thumb/.../220px-File.jpg`
  returns `HTTP 400` for arbitrary widths. The service rewrites these to the
  original file URL automatically. If you need a specific size, use one of
  Wikimedia's listed thumbnail sizes.
- **`pbs.twimg.com`:** occasionally rejects a cross-origin `Referer`; the
  downloader retries without it, then falls back to the Wikimedia original when
  applicable.
- **SVG is not accepted** by X; supply a raster image.
- `media_id` is a numeric ID from X. Use it only with the same authenticated
  session that uploaded it.

### Notifications

- X's notification payload mixes many templates. Fields such as `message`,
  `icon`, and `timestampMs` are optional, and some notification types have no
  resolvable actor.
- The parser skips malformed entries instead of failing the whole feed.
- Each item includes `kind` (X's icon id). A `recommendation_icon` item is a
  recommendation, not an interaction: it has a `message` topic and ids in
  `from_user.id` / `tweet_id`, but X does not inline the sender profile, so
  `screen_name` and `name` are empty. That is a payload limitation, not a
  parsing bug.
- Use `get_notifications(raw=true)` to inspect X's unparsed objects.
- Do not build a digest that **requires** `get_notifications`; prefer
  `get_mentions`, `search_tweets`, or `get_home_timeline` as primaries.

### Followers / following

- The GraphQL `Followers` query ID has returned `404` for every account at
  times. The service automatically falls back to the v1.1 REST list
  (`followers/list.json` / `friends/list.json`) and maps the legacy payload
  directly.
- `count` is advisory upstream; the service trims the returned page.

### Search

- If the authed search path is rejected or unavailable, the service retries with
  the guest search endpoint before failing.
- Valid empty results are returned as `[]`, not as errors.

### Trends

- X no longer populates per-trend volume, so `tweet_count` is always `null`.
  Use the 1-based `rank` for ordering.

### Likes

- X restricts the Likes timeline for non-owner accounts. `get_user_likes` often
  returns an empty list (which the MCP layer renders as empty content) even for
  accounts with clearly public activity. Treat it as **unreliable for
  third-party accounts**, not as an error.

---

## Recommended retry policy

For agent/cron callers:

```text
reads:   2 attempts, 1–2 s backoff, retry only on transport/5xx/429
writes:  1 attempt; inspect the error; retry only when the operation is
         idempotent (like, retweet, follow, bookmark) or when you have
         confirmed the side effect did not happen (e.g. re-read the timeline)
media:   uploads may be retried on transport/5xx; content errors never
```

The server itself retries transient X errors up to 3 times with exponential
backoff for most calls. Client-side retries should therefore be limited; a
client timeout is usually a Render cold start, not an X error.

---

## Render / hosting notes

- The service runs on Render. Free/low tiers spin down and can produce
  `Socket timeout` at the client even when the request would have succeeded.
- The MCP transport is stateless; there is no sticky session. Concurrent calls
  are independent and can contend for X rate limits.
- If cold starts are a problem for scheduled jobs, keep the service warm or
  move to a plan without spin-down.

Useful checks:

```bash
render services list
render deploys list srv-dahac1e1egvs73d8ie20 --output text
render logs --resources srv-dahac1e1egvs73d8ie20 --limit 100 --output text
curl -s https://twitter-api-gjyc.onrender.com/health
```

---

## Verification log

| Date | Deploy | Verified |
|---|---|---|
| 2026-11-08 | `c401690` | Full 30-tool sweep green; `get_notifications(raw=true)` inspected (recommendation notification: `kind=recommendation_icon`, topic `K2S`, ids present, no inlined profile); `send_dm` returns X's real code 476 |
| 2026-11-08 | `c75a3a6` | `upload_media` with a 400-ing Wikimedia thumb URL and a `pbs.twimg.com` URL; `post_tweet_with_media` end-to-end (tweet deleted); `post_tweet` (tweet deleted); `search_tweets`; `get_notifications`; `get_followers` count=3; `get_bookmarks`; `unbookmark_tweet` (test bookmark removed); clear error text for a non-image URL and an invalid DM recipient |
| 2026-11-08 | `3c26ab9` | `get_followers` v1.1 REST fallback (GraphQL 404); `count` enforcement on followers/following/user tweets/home timeline |

---

## Smoke-test recipe

Minimal, read-only check after a deploy (replace the tool/args as needed):

```bash
python3 - <<'PY'
import json, httpx
URL = "https://twitter-api-gjyc.onrender.com/mcp/"
H = {"Content-Type": "application/json",
     "Accept": "application/json, text/event-stream"}

def rpc(p):
    r = httpx.post(URL, headers=H, json=p, timeout=180)
    r.raise_for_status()
    return r.json()

rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "1"}}})
rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})

def call(name, args):
    r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": name, "arguments": args}})
    res = r["result"]
    print(name, "ERROR" if res.get("isError") else "OK",
          res["content"][0]["text"][:200])

call("get_trends", {"count": 3})
call("get_user", {"screen_name": "openai"})
call("get_notifications", {"count": 3})
PY
```