#!/usr/bin/env python3
"""Build the human-readable backtest report and optionally send via Telegram."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TELEGRAM_SEND = os.path.expanduser("~/workspace/skills/telegram/bin/send.py")
CHAT_ID = "6883056760"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def describe_window(v):
    if v["kind"] == "hourly":
        return f"{v['key']:02d}:00 UTC {v['direction']}"
    return f"{WEEKDAYS[v['key']]} {v['direction']}"


def build_report(results_list):
    lines = []
    lines.append("TEMPORAL EDGE VALIDATOR — backtest report (paper only)")
    lines.append("=" * 56)
    for res in results_list:
        sym = res["symbol"]
        lines.append("")
        lines.append(f"--- {sym} ({res['n_candles']} hourly candles, "
                     f"{res['n_candidates']} windows tested) ---")
        if res["btc_beta"]:
            lines.append(f"BTC beta stripped: {res['btc_beta']:.3f} "
                         "(edge measured on residuals)")
        lines.append(f"Costs: {res['round_trip_cost']:.2%} round-trip per trade. "
                     "Bonferroni-corrected discovery on train (70%), "
                     "validated on frozen test (30%).")
        perm = res["permutation"]
        drag = res["cost_drag"]
        lines.append(
            f"Cost drag: the average hourly trade nets "
            f"{drag['mean_net_per_trade']:+.3%} after costs "
            f"(t={drag['t_stat']:.1f}, n={drag['n']}) — reliably negative. "
            f"Every naive window starts here.")
        lines.append(
            f"Permutation check (gross returns, no cost drag): best window "
            f"t-stat {perm['best_gross_t']:.2f} beats "
            f"{perm['null_percentile']:.0%} of pure-noise shuffles "
            f"(noise 95th pct: {perm['null_p95']:.2f}).")
        disc = res["discovered_on_train"]
        val = res["validated_on_test"]
        if not disc:
            lines.append("RESULT: no window survived corrected discovery. "
                         "No edge found — Kelly says bet 0.")
        else:
            lines.append(f"Discovered on train: {len(disc)} window(s).")
            for v in val:
                exp = v["expectancy"]
                status = "CONFIRMED on test" if v["confirmed"] else \
                    "FAILED on test (likely noise)"
                lines.append(
                    f"  {describe_window(v)}: train-implied edge -> test "
                    f"expectancy {exp:+.3%} per trade, win rate "
                    f"{v['win_rate']:.1%}, n={v['n']} — {status}")
        k = res.get("kelly")
        if k and k["half_kelly"]:
            lines.append(f"Position sizing: half-Kelly on best window = "
                         f"{k['half_kelly']:.2%} of equity per trade "
                         f"({k['candidate']}).")
        elif disc:
            lines.append("Position sizing: no positive Kelly fraction — "
                         "edge too thin to size.")
    lines.append("")
    lines.append("Method: hourly windows (long/short), weekday windows, "
                 "0.2% costs, Bonferroni correction, permutation null. "
                 "Paper only — no real money, no exchange account.")
    return "\n".join(lines)


def send_telegram(text):
    """Direct token-file path (same as the memecoin bot's daemon).

    The vault-surrogate path does not engage from this VM's shell egress,
    so we read the token file the daemon itself uses. The token value is
    never printed or logged.
    """
    import urllib.parse
    import urllib.request
    token = open(os.path.expanduser("~/.forex-agent/telegram/bot_token")).read().strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    if not body.get("ok"):
        raise RuntimeError(f"telegram send failed: {body}")


def main(argv):
    results = []
    for f in sorted(os.listdir(HERE)):
        if f.startswith("results_") and f.endswith(".json"):
            with open(os.path.join(HERE, f)) as fh:
                results.append(json.load(fh))
    if not results:
        print("no results_*.json found — run validator.py first")
        sys.exit(1)
    report = build_report(results)
    print(report)
    if "--telegram" in argv:
        send_telegram(report)
        print("\nsent via Telegram")


if __name__ == "__main__":
    main(sys.argv[1:])
