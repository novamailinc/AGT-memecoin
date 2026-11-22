"""Watch for the first live tranche mirror transfer (up to 60 minutes).
Prints the transfer + tranche demo account state when one fires.
Durable path: /tmp is wiped on runtime restarts, so this lives in ~/workspace."""
import json, time, datetime

deadline = time.time() + 60 * 60
while time.time() < deadline:
    try:
        d = json.load(open('/home/hatch/.forex-agent/devnet_demo_ledger.json'))
        tr = [t for t in d.get('transfers') or [] if t.get('lane') == 'tranche']
        if tr:
            t = json.load(open('/home/hatch/.forex-agent/devnet_demo_trading_ledger.json'))
            sub = (t.get('lanes') or {}).get('tranche', {})
            print('TRANCHE TRANSFER FIRED:')
            for x in tr:
                ts = x.get('confirmed_ts') or 0
                w = datetime.datetime.fromtimestamp(ts + 3600, datetime.timezone.utc).strftime('%H:%M WAT') if ts else '-'
                print(' ', x.get('mint', ''), x.get('sig', ''), w)
            print('tranche demo counts:', sub.get('counts'))
            print('tranche demo open:', list((sub.get('open_positions') or {}).keys()))
            break
    except Exception as e:
        print('check error:', e)
    time.sleep(60)
else:
    print('TIMEOUT: no tranche transfer in 60 minutes')
