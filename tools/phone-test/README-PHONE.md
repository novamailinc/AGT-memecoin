# Phone test — $6 live grad_select trial (Termux, Android)

This is the burst test while the Oracle server waits on the $1 card hold.
The phone CANNOT run 24/7 (Android sleeps, Termux gets killed in the
background). The phone test proves the live executor works on real
mainnet with real money, in short sessions with the phone awake and
charging. The server is still the 24/7 home later.

## Step 1 — Install Termux

Install Termux from **F-Droid** (https://f-droid.org/packages/com.termux/).
Do NOT use the Play Store version — it is old and broken.
This guide is for Android. iPhone cannot run this test.

## Step 2 — Download this bundle on the phone

Download the .tar.gz link Nova sent you, then in Termux:

    cd ~
    tar -xzf live-phone-test.tar.gz
    cd live-phone-test

## Step 3 — Run the setup

    bash phone-setup.sh

It installs Python + solders, creates a FRESH dedicated wallet at
`~/.forex-agent/live/keypair.json` (0600, never overwritten if it
already exists), prints the wallet address, and writes the deployment
marker with `armed: false` (dry-run only).

**The key file is your wallet. Never share it, never send it to
anyone (including Nova), never paste it anywhere.**

## Step 4 — Fund it

Send about **$7 of SOL** to the printed wallet address from your own
Phantom wallet: $6 to trade, about $1 stays back for fees. Nothing
else ever goes into this wallet. Worst loss is the $7.

## Step 5 — Dry-run (no money moves)

    MEMECOIN_WALLET_PUBKEY=<the printed address> python3 scripts/live-dry-run <a liquid coin mint> --count 5

Use the mint of any big liquid coin (for example a BONK or JUP mint).
It builds real Jupiter quotes and simulates real swaps without
sending anything. It must show the buy simulations succeeding.

## Step 6 — Shakedown, with Nova watching

Tell Nova the dry-run passed. Then, together: arm the marker and run
one ~$0.50 buy of an already-graduated liquid coin, sell it through
one ladder leg, and record the real fees and slippage. Only then does
the $6 challenge start.

## Running the trader (burst only)

    termux-wake-lock
    python3 scripts/grad-select-live-trader --arm

Keep the phone charging, Termux in the foreground, screen on. Stop
it with Ctrl+C when the session ends. `termux-wake-lock` only helps
while Termux stays alive — it does not make the phone 24/7.

## Safety (same rules as the server build)

- $1 per coin, at most 6 open, $6 trading cap, $3 daily loss cap.
- Killswitch lane `grad_select_live` halts entries on negative
  expectancy; exits always drain while halted.
- Paper bot untouched — this is a separate process and ledger.
- No mainnet key is ever created on Nova's machine. The key lives
  only on this phone.
