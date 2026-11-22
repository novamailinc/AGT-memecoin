"""Tests for realdata.py -- run against sample_ledgers.json, a small
fixture built from the real lane ledgers (raw shapes preserved, fields
whitelisted; entry snapshots dropped entirely because the real ones
carry wallet-name fragments -- the private wallet data stays out).

Hand-computed expectations (see fixture):
  paper (3): 3.027, 0.1493, 0.024 -> exp +0.0668, WR 1/3, rugs 1/3,
             one round-tripper (0.1493 after a 2.1229 peak)
  degen_mirror (3): leg sums 1.166224, 0.193444, 0.191459 on $1 shots
             -> exp -0.4830, no recorded peaks (peak unknown x3)
  ceo (1): recorded realized_mult 1.4047 -> exp +0.4047, peak unknown
  steady (3): 1 + pnl/1.0 = 1.5611, 0.8321, 0.7291 -> exp +0.0408
  stacker (3): 1 + pnl/3.0 = -0.05, 0.6576, 0.5531 -> exp -0.6131
             (the -0.05 close is the recorded -$3.15 on a $3.00 size)
"""
import json
import os

import pytest

import realdata

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = json.load(open(os.path.join(HERE, "sample_ledgers.json")))


def _paths(tmp_path):
    ds = tmp_path / "dataset.jsonl"
    with open(ds, "w") as handle:
        for rec in FIXTURE["paper_closes"]:
            handle.write(json.dumps(rec) + "\n")
    paths = {"dataset": str(ds)}
    for key in ("mirror", "ceo", "steady", "stacker"):
        target = tmp_path / f"{key}.json"
        target.write_text(json.dumps(FIXTURE[key]))
        paths[key] = str(target)
    return paths


@pytest.fixture()
def closes(tmp_path):
    return realdata.load_all_closes(_paths(tmp_path))


def test_loads_all_thirteen_sample_closes(closes):
    assert len(closes) == 13
    lanes = [c["lane"] for c in closes]
    assert lanes.count("paper") == 3
    assert lanes.count("degen_mirror") == 3
    assert lanes.count("ceo") == 1
    assert lanes.count("steady") == 3
    assert lanes.count("stacker") == 3


def test_mirror_realized_is_the_leg_sum_on_a_dollar(closes):
    mirror = [c for c in closes if c["lane"] == "degen_mirror"]
    assert mirror[0]["realized_mult"] == pytest.approx(1.041356 + 0.062434 + 0.062434)
    assert mirror[1]["realized_mult"] == pytest.approx(0.193444)
    # Cross-check against the recorded pnl: leg sum ~= 1 + pnl_usd.
    assert mirror[0]["realized_mult"] == pytest.approx(1 + 0.1662, abs=1e-3)


def test_peak_unknown_is_explicit_for_mirror_and_ceo(closes):
    card = realdata.full_scorecard(closes)
    assert card["n_peak_unknown"] == 4  # 3 mirror + 1 ceo
    assert card["n_peak_known"] == 9
    assert card["per_lane"]["degen_mirror"]["n_peak_unknown"] == 3
    assert card["per_lane"]["ceo"]["n_peak_unknown"] == 1


def test_per_lane_scorecard_hand_computed(closes):
    card = realdata.full_scorecard(closes)
    paper = card["per_lane"]["paper"]
    assert paper["n"] == 3 and paper["wr"] == pytest.approx(1 / 3)
    assert paper["expectancy"] == pytest.approx(0.0668, abs=1e-3)
    assert paper["rug_rate"] == pytest.approx(1 / 3)
    assert paper["rug_basis"] == "recorded"
    assert paper["round_trippers"] == 1
    steady = card["per_lane"]["steady"]
    assert steady["expectancy"] == pytest.approx(0.0408, abs=1e-3)
    stacker = card["per_lane"]["stacker"]
    assert stacker["expectancy"] == pytest.approx(-0.6131, abs=1e-3)
    ceo = card["per_lane"]["ceo"]
    assert ceo["expectancy"] == pytest.approx(0.4047)


def test_totals_and_leakage_over_peak_known_only(closes):
    card = realdata.full_scorecard(closes)
    assert card["n"] == 13
    assert card["wr"] == pytest.approx(2 / 13)
    assert card["expectancy"] == pytest.approx(-0.1970, abs=1e-3)
    leak = card["leakage_peak_known_only"]
    assert leak["n_peaked_2x"] == 2          # paper peaks 3.027, 2.1229
    assert leak["n_round_tripped"] == 1
    assert leak["avg_realized_round_trippers"] == pytest.approx(0.1493)


def test_render_markdown_has_all_lanes_and_total(closes):
    text = realdata.render_markdown(closes)
    for lane in ("paper", "degen_mirror", "ceo", "steady", "stacker", "TOTAL"):
        assert lane in text
    assert "peak" in text  # the peak-unknown caveat is rendered, not hidden


def test_missing_sources_are_skipped_not_invented(tmp_path):
    assert realdata.load_all_closes({
        "dataset": str(tmp_path / "nope.jsonl"),
        "mirror": str(tmp_path / "nope.json"),
        "ceo": str(tmp_path / "nope.json"),
        "steady": str(tmp_path / "nope.json"),
        "stacker": str(tmp_path / "nope.json"),
    }) == []
