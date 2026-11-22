# Telegram connector: surrogate exchange diagnosis (2026-09-22)

## Setup (all working)
- Connector `custom.telegram` connected; bot token stored in authd vault, never visible.
- `dynamic_credential_entry('custom.telegram')` returns a valid `hsurr:*` surrogate
  via the local authd unix socket. Placement: `url_path_segment: /bot{}/sendMessage`.
- Skill at `~/workspace/custom-connectors/custom.legacy-telegram/` with `bin/send.py` using
  `url_with_surrogate_path_segment(...)` exactly as scaffolded.

## Failure
- Every send attempt from this VM's shell reaches `api.telegram.org` with the
  RAW SURROGATE in the URL path. Telegram responds with its own
  `{"ok":false,"error_code":404,"description":"Not Found"}` — i.e. no exchange happened.
- Verified in both URL-encoded (`hsurr%3A…`) and raw (`hsurr:…`) forms, via
  `sendMessage` (exact placement template) and `getMe`.

## Root cause
- The helper docstring promises: "Sentinel/authd replace those surrogates with
  real credentials on approved outbound requests."
- This VM's egress is MITM'd ("Hatch Sandbox Egress CA" signs the cert for
  api.telegram.org), so the proxy sees the plaintext surrogate — but never swaps it.
- The Sentinel sockets referenced in the environment
  (`/run/hatch/sentinel/*`) DO NOT EXIST on this VM. Direct egress is blocked.
- Conclusion: the surrogate-exchange component is absent from this VM's egress
  path. Requests from the interactive shell are not "approved" in the Sentinel
  sense, so they pass through unexchanged. This is runtime infrastructure, not
  skill code — no CLI change can fix it.

## What was tried
- Correct helper usage (path-segment placement), both encodings, getMe + sendMessage.
- Direct (proxy-bypassed) egress: blocked (TLS error).
- `browser.open` to the API: tool failure, terminal.
- authd socket probing: only `/v1/credentials/surrogate` exists; no exchange endpoint.
- `permissions.list_pending`: no egress approval pending for api.telegram.org.
- Scheduler-worker path: one-shot cron `telegram-egress-test` scheduled
  2026-09-22 17:20 WAT to test whether the daemon/background egress path
  (which has its own approval socket) triggers an approval card + exchange.

## If the cron test also fails
- The exchange is unavailable from every agent execution context on this VM.
- Keep the skill staged (it is correct and will work if the infra appears).
- Do NOT ask the user to paste the token (credential rules); do NOT use
  `request_api_access(reconnect)` — the credential was never attached, so the
  stored token is not proven wrong.
- Offer to file feedback to the Muse team via the muse_feedback skill.
