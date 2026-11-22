# Chainstack

- `custom.chainstack` connects Muse to Chainstack through its Solana JSON-RPC API. It authenticates with the registered `api_key` credential and calls only `solana-mainnet.core.chainstack.com`.

- Helpers live in `~/workspace/custom-connectors/custom.chainstack/bin/`. `chainstack.py` sends Solana JSON-RPC calls and prints the JSON response.

## Connect

- The connection was registered with `credentials.request_api_access` and these details. Reuse them to reconnect. The placement cannot be read back later, so keep it here:

```json
{
  "provider": "chainstack",
  "auth_scheme": "api_key",
  "placement": "url_path_segment:/{}",
  "api_hosts": ["solana-mainnet.core.chainstack.com"]
}
```

- Before the first change in a conversation, prove access with a harmless read:

```text
python3 ~/workspace/custom-connectors/custom.chainstack/bin/chainstack.py health
```

- The first health read on 2026-11-22 returned HTTP 401 Unauthorized. The surrogate was verified as issued and placed in the URL path segment, so the request carried the credential and Chainstack rejected the key value. Re-enter just the API key, not the full endpoint URL. Do not enter the full endpoint URL as the key.

## Credentials

- The registered connection holds the credential. Helpers import `/opt/muse/skills/custom-connector-setup/bin/dynamic_credentials.py`, which returns an `hsurr:` surrogate and places it where the provider reads the key. Muse swaps in the real credential only on an approved request to a registered host.

- Never put a key, token, password or cookie in this doc, a helper, a command argument, an environment variable or a log. Never ask the user to paste one in chat.

- Keep redirects disabled on authenticated requests.

- On a 401 or 403, check the request first. Confirm that the helper attached the credential, that the host is registered and that the placement and scopes match.

- Only when a request that carried the credential is still rejected, call `credentials.request_api_access` with the details above and `"reconnect": true`.

## Read node health

- `health` sends the `getHealth` JSON-RPC call and prints the result. Use it as the harmless read that proves access. It changes nothing.

```text
python3 ~/workspace/custom-connectors/custom.chainstack/bin/chainstack.py health
```

- A healthy node returns `{"jsonrpc": "2.0", "result": "ok", "id": 1}`. Attempted 2026-11-22, returned HTTP 401, key rejected.

## Read the current slot

- `slot` sends the `getSlot` JSON-RPC call and prints the result. Use it to confirm the node is live and in sync. It changes nothing.

```text
python3 ~/workspace/custom-connectors/custom.chainstack/bin/chainstack.py slot
```

- Untested while the key is rejected.

## Send any Solana JSON-RPC call

- `call` sends the named method with the given JSON params and prints the result. Use it for the calls the bot needs, such as `getSignaturesForAddress`, `getTransaction`, `getAccountInfo` and `getBalance`.

```text
python3 ~/workspace/custom-connectors/custom.chainstack/bin/chainstack.py call <method> [params-json]
```

- Untested while the key is rejected.

## Recover from errors

- HTTP 401 means Chainstack rejected the key. Check that only the API key was entered, not the full endpoint URL, and that the key belongs to a Solana mainnet node. Then reconnect with the details above and `"reconnect": true`.

- HTTP 429 means the free tier rate limit was hit. Wait and retry with backoff. Do not hammer the endpoint.

- After a failed or timed-out change, check the current state with a read before trying again, because the first request may have completed.

## Limits

- Developer Free is 3M request units a month and 5 requests a second on Solana mainnet. `getSignaturesForAddress` is effectively unavailable on the free tier because it is always billed as archive and Developer has no archive data. `getProgramAccounts` is paid only. Nodes are auto-deleted after 30 days of inactivity.

- This connector only reaches the Solana mainnet JSON-RPC host. It does not reach other Chainstack networks or hosts.

- The key was rejected on the first call, so no operation in this doc is verified against a live Chainstack node yet.

## Related skills

- Use `muse.skill_search` to find other skills and `muse.connector_search` to find other connectors.
