"""Demo for signal-refinery lab. Synthetic only."""
from refinery import validate_record, FeedSwitches, TapJournal, TapOutcome, source_name


def main() -> None:
    print("=== Signal refinery lab (four chains, one destination) ===")
    good = {"schema": 1, "chain": "base", "source": "br", "mint": "0xabc123", "ts": 1700000000, "score": 72}
    bad = {"schema": 1, "chain": "mars", "source": "xx", "mint": "x", "ts": -1, "score": 999}
    print(f"Valid record: {validate_record(good)}")
    print(f"Invalid record: {validate_record(bad)}")
    print(f"Source codes: br={source_name('br')} hl={source_name('hl')} ll={source_name('ll')}")
    switches = FeedSwitches()
    j = TapJournal()
    for i in range(10):
        j.add(TapOutcome(chain="base", mint=f"m{i}", closed=True, peak_mult=1.1))
    j.add(TapOutcome(chain="base", mint="winner", closed=True, peak_mult=2.5))
    print(f"Base taps closed={j.tap_count('base')} win_rate={j.win_rate('base'):.3f}")
    print(f"Auto-pause evaluation: {switches.evaluate_auto_pause(j)} base_paused={switches.is_paused('base')}")
    print(f"Solana pause attempt: {switches.pause('solana')} paused={switches.is_paused('solana')} flags={switches.flags}")
    print(f"Auto-resume attempt: {switches.resume('base', by='auto')} still_paused={switches.is_paused('base')}")
    print(f"Human resume: {switches.resume('base', by='human')} paused={switches.is_paused('base')}")


if __name__ == "__main__":
    main()
