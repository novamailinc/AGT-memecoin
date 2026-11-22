#!/data/data/com.termux/files/usr/bin/bash
# Phone setup for the $6 live test (Termux, Android).
# Run ONCE from inside the extracted bundle folder:
#     bash phone-setup.sh
#
# What it does:
#   1. Installs Python (if missing) and the ONE package the live
#      trader needs: solders (Solana signing).
#   2. Creates ~/.forex-agent/live/
#   3. Creates a FRESH dedicated wallet (keypair.json, 0600) — ONLY
#      if one does not already exist. It NEVER overwrites an
#      existing wallet. The secret never leaves this phone and is
#      never printed.
#   4. Writes deployment.json with armed=false (dry-run only until
#      you arm it yourself).
#
# After this: fund the printed wallet address with about $7 of SOL
# ($6 to trade + about $1 stays back for fees), then run the dry-run.
set -e
echo "== phone setup: live $6 test =="
pkg update -y >/dev/null 2>&1 || true
command -v python3 >/dev/null || pkg install -y python
python3 -m pip install --quiet --upgrade pip
python3 -m pip install --quiet "solders==0.29.0"
LIVE_DIR="$HOME/.forex-agent/live"
mkdir -p "$LIVE_DIR"
KEYFILE="$LIVE_DIR/keypair.json"
if [ -f "$KEYFILE" ]; then
  echo "Wallet already exists at $KEYFILE — keeping it, NOT overwriting."
else
  python3 - "$KEYFILE" <<'PYEOF'
import json, os, sys
from solders.keypair import Keypair
path = sys.argv[1]
kp = Keypair()
with open(path, "w") as f:
    json.dump(list(kp.to_bytes()), f)
os.chmod(path, 0o600)
print("NEW WALLET ADDRESS (fund THIS address, nothing else):")
print(str(kp.pubkey()))
PYEOF
fi
MARKER="$LIVE_DIR/deployment.json"
if [ -f "$MARKER" ]; then
  echo "Deployment marker already exists at $MARKER — keeping it."
else
  printf '{\n  "machine": "phone-termux",\n  "armed": false\n}\n' > "$MARKER"
  echo "Wrote $MARKER (armed: false — dry-run only for now)."
fi
echo ""
echo "NEXT STEPS:"
echo "  1. Send about \$7 of SOL to the wallet address above"
echo "     (\$6 to trade, about \$1 stays back for fees)."
echo "     Send it from your own Phantom wallet. Do NOT send from"
echo "     your main wallet to any other address."
echo "  2. Dry-run (no money moves):"
echo "     MEMECOIN_WALLET_PUBKEY=<the address above> \\"
echo "       python3 scripts/live-dry-run <a liquid coin mint> --count 5"
echo "  3. Tell Nova the dry-run passed, and we do the first real"
echo "     ~\$0.50 test trade together, with you watching."
echo ""
echo "WARNING: $KEYFILE is your wallet. Never share it, never send"
echo "it to anyone (including Nova), never paste it anywhere."
