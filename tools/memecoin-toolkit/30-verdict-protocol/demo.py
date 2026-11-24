"""Demo for verdict-protocol lab. Synthetic only."""
from verdict import decide, degen_v2_fixture, small_sample_fixture, halted_394_fixture, all_green_fixture, required_n, LaneRecord, Gates


def main() -> None:
    print("=== Lane verdict protocol lab (synthetic lanes) ===")
    print(f"Required closes to separate 35% WR from 30%: {required_n()}")
    for make in (degen_v2_fixture, small_sample_fixture, halted_394_fixture, all_green_fixture):
        lane, gates = make()
        print(decide(lane, gates).render())
    lane = LaneRecord("promising", closes=300, expectancy=0.08, win_rate=0.36, weeks_of_data=6.0)
    print(decide(lane, Gates(True, True, False, True)).render())


if __name__ == "__main__":
    main()
