# Alchemy

- `custom.alchemy` connects Muse to Alchemy through its Solana JSON-RPC API. It authenticates with the registered `api_key` credential and calls only `solana-mainnet.g.alchemy.com`.

- Helpers live in `~/workspace/custom-connectors/custom.alchemy/bin/`. `alchemy.py` sends Solana JSON-RPC calls and prints the JSON response.

## Connect

- The connection was registered with `credentials.request_api_access` and these details. Reuse them to reconnect. The placement cannot be read back later, so keep it here:

```json
{
  "provider": "alchemy",
  "auth_scheme": "api_key",
  "placement": "url_path_segment:/v2/{}",
  "api_hosts": ["solana-mainnet.g.alchemy.com"]
}
```

- Before the first change in a conversation, prove access with a harmless read:

```text
python3 ~/workspace/custom-connectors/custom.alchemy/bin/alchemy.py health
```

- The first health read on 2026-11-22 returned HTTP 401 Unauthorized. The surrogate was verified as issued with placement `url_path_segment /v2/{}`, so the request carried the credential and Alchemy rejected the key value. Re-enter the Solana mainnet API key from the Alchemy app with Solana enabled. Do not enter the full endpoint URL as the key.

## Credentials

- The registered connection holds the credential. Helpers import `/opt/muse/skills/custom-connector-setup/bin/dynamic_credentials.py`, which returns an `hsurr:` surrogate and places it where the provider reads the key. Muse swaps in the real credential only on an approved request to a registered host.

- Never put a key, token, password or cookie in this doc, a helper, a command argument, an environment variable or a log. Never ask the user to paste one in chat.

- Keep redirects disabled on authenticated requests.

- On a 401 or 403, check the request first. Confirm that the helper attached the credential, that the host is registered and that the placement and scopes match.

- Only when a request that carried the credential is still rejected, call `credentials.request_api_access` with the details above and `"reconnect": true`.

## Read node health

- `health` sends the `getHealth` JSON-RPC call and prints the result. Use it as the harmless read that proves access. It changes nothing.

```text
python3 ~/workspace/custom-connectors/custom.alchemy/bin/alchemy.py health
```

- A healthy node returns `{"jsonrpc": "2.0", "result": "ok", "id": 1}`. Untested while the key is rejected, the helper itself compiles and runs.

## Read the current slot

- `slot` sends the `getSlot` JSON-RPC call and prints the result. Use it to confirm the node is live and in sync. It changes nothing.

```text
python3 ~/workspace/custom-connectors/custom.alchemy/bin/alchemy.py slot
```

- Untested while the key is rejected.

## Send any Solana JSON-RPC call

- `call` sends the named method with the given JSON params and prints the result. Use it for the calls the bot needs, such as `getSignaturesForAddress`, `getTransaction`, `getAccountInfo` and `getBalance`.

```text
python3 ~/workspace/custom-connectors/custom.alchemy/bin/alchemy.py call <method> [params-json]
```

- Untested while the key is rejected.

## Recover from errors

- HTTP 401 means Alchemy rejected the key. Check that the key is the Solana mainnet API key from an Alchemy app with Solana enabled, and that only the key was entered, not the full endpoint URL. Then reconnect with the details above and `"reconnect": true`.

- HTTP 429 means the free tier rate limit was hit. Wait and retry with backoff. Do not hammer the endpoint.

- After a failed or timed-out change, check the current state with a read before trying again, because the first request may have completed.

## Limits

- The free tier has rate limits and monthly compute unit caps. Keep call volume inside them.

- This connector only reaches the Solana mainnet JSON-RPC host. It does not reach other Alchemy networks or hosts.

- The key was rejected on the first call, so no operation in this doc is verified against a live Alchemy node yet.

## Related skills

- Use `muse.skill_search` to find other skills and `muse.connector_search` to find other connectors.
