"""Tests for bundle.py -- union-find, share math, verdict bands."""
from bundle import (classify_behavior, cluster_share, evaluate,
                    evaluate_cluster, find_clusters, verdict_for_share)

SUPPLY = 1_000_000  # 1% == 10_000 units


def test_union_find_transitivity_across_two_funders():
    wallets = [
        {"wallet": "A", "funder": "F1"},
        {"wallet": "B", "funders": ["F1", "F2"]},
        {"wallet": "C", "funder": "F2"},
        {"wallet": "D", "funder": "F9"},
    ]
    clusters = find_clusters(wallets)
    assert ["A", "B", "C"] in clusters
    assert ["D"] in clusters
    assert len(clusters) == 2


def test_wallets_sharing_one_funder_cluster_together():
    wallets = [{"wallet": w, "funder": "SAME"} for w in ("X", "Y", "Z")]
    assert find_clusters(wallets) == [["X", "Y", "Z"]]


def test_unfunded_wallets_are_singletons_and_bad_rows_skipped():
    wallets = [{"wallet": "LONE"}, {"funder": "F"}, {"wallet": "LONE2"}]
    clusters = find_clusters(wallets)
    assert clusters == [["LONE"], ["LONE2"]]


def test_cluster_share_math():
    balances = {"A": 100_000, "B": 50_000, "C": 999_000}
    assert cluster_share(["A", "B"], balances, SUPPLY) == 15.0
    assert cluster_share(["A", "MISSING"], balances, SUPPLY) == 10.0


def test_cluster_share_bad_supply_is_none():
    assert cluster_share(["A"], {"A": 1}, 0) is None
    assert cluster_share(["A"], {"A": 1}, None) is None


def test_behavior_distributing_at_exactly_half():
    flows = {"A": {"in": 1, "out": 9}, "B": {"in": 9, "out": 1}}
    assert classify_behavior(flows) == "distributing"  # 1*2 >= 2


def test_behavior_minority_distributing_is_holding():
    flows = {"A": {"in": 1, "out": 9}, "B": {"in": 9, "out": 1},
             "C": {"in": 8, "out": 2}}
    assert classify_behavior(flows) == "holding"  # 1*2 < 3


def test_behavior_no_distributors_is_holding():
    flows = {"A": {"in": 10, "out": 0}, "B": {"in": 5, "out": 5}}
    assert classify_behavior(flows) == "holding"


def test_behavior_unknown_when_no_activity_or_no_data():
    assert classify_behavior(None) == "unknown"
    assert classify_behavior({}) == "unknown"
    assert classify_behavior({"A": {"in": 0, "out": 0}}) == "unknown"
    assert classify_behavior({"A": {}}) == "unknown"


def test_behavior_list_form_and_long_key_names():
    flows = [{"wallet": "A", "inflows": 0, "outflows": 7}]
    assert classify_behavior(flows) == "distributing"


def test_verdict_extreme_share_vetoes_regardless_of_behavior():
    for behavior in ("distributing", "holding", "unknown"):
        verdict, reason = verdict_for_share(50.0, behavior)
        assert verdict == "VETO"
        assert reason == "extreme share"
    assert verdict_for_share(75.0, "holding")[0] == "VETO"


def test_verdict_middle_band_distributing_vetoes():
    assert verdict_for_share(20.0, "distributing")[0] == "VETO"
    assert verdict_for_share(49.99, "distributing")[0] == "VETO"


def test_verdict_middle_band_unknown_behavior_is_flag_not_veto():
    assert verdict_for_share(30.0, "unknown")[0] == "FLAG"
    assert verdict_for_share(30.0, "holding")[0] == "FLAG"


def test_verdict_below_20_passes():
    assert verdict_for_share(19.99, "distributing")[0] == "PASS"
    assert verdict_for_share(0.0, "unknown")[0] == "PASS"


def test_evaluate_end_to_end_picks_largest_cluster():
    wallets = [
        {"wallet": "A", "funder": "F1"},
        {"wallet": "B", "funders": ["F1", "F2"]},
        {"wallet": "C", "funder": "F2"},
        {"wallet": "D", "funder": "F9"},
    ]
    balances = {"A": 100_000, "B": 80_000, "C": 70_000, "D": 10_000}
    flows = {"A": {"in": 0, "out": 5}, "B": {"in": 0, "out": 3},
             "C": {"in": 4, "out": 0}, "D": {"in": 9, "out": 0}}
    result = evaluate(wallets, balances, SUPPLY, flows)
    assert result.cluster == ["A", "B", "C"]
    assert result.share_pct == 25.0
    assert result.behavior == "distributing"
    assert result.verdict == "VETO"
    assert result.vetoed is True


def test_evaluate_no_wallets_or_bad_supply_is_unknown():
    assert evaluate([], {}, SUPPLY).verdict == "UNKNOWN"
    result = evaluate_cluster(["A"], {"A": 10}, 0)
    assert result.verdict == "UNKNOWN"
    assert result.share_pct is None


# -- UnionFind directly ------------------------------------------------------

from bundle import (UnionFind, cluster_shares, summarize_clusters,
                    worst_verdict)


def test_unionfind_union_is_idempotent_and_self_is_own_root():
    uf = UnionFind()
    uf.add("A")
    assert uf.find("A") == "A"
    uf.union("A", "B")
    root = uf.find("A")
    uf.union("A", "B")  # again: no change, no error
    uf.union("B", "A")
    assert uf.find("B") == root
    assert uf.find("A") == root


def test_unionfind_disjoint_sets_and_lazy_add_on_find():
    uf = UnionFind()
    uf.union("A", "B")
    uf.union("C", "D")
    assert uf.find("A") != uf.find("C")
    assert uf.find("NEVER_ADDED") == "NEVER_ADDED"  # find adds lazily


def test_unionfind_long_union_chain_shares_one_root():
    uf = UnionFind()
    for a, b in (("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")):
        uf.union(a, b)
    assert len({uf.find(w) for w in "ABCDE"}) == 1


# -- find_clusters edges -------------------------------------------------------


def test_find_clusters_duplicate_wallet_rows_are_deduplicated():
    wallets = [{"wallet": "A", "funder": "F"},
               {"wallet": "A", "funder": "F"},
               {"wallet": "B", "funder": "F"}]
    assert find_clusters(wallets) == [["A", "B"]]


def test_find_clusters_plain_string_wallets_are_singletons():
    assert find_clusters(["A", "B"]) == [["A"], ["B"]]


def test_find_clusters_address_owner_keys_and_funded_by():
    wallets = [{"address": "A", "funder": "F"},
               {"owner": "B", "funder": "F"},
               {"wallet": "C", "funded_by": "F"}]
    assert find_clusters(wallets) == [["A", "B", "C"]]


def test_find_clusters_funders_tuple_and_single_string_forms():
    wallets = [{"wallet": "A", "funders": ("F1", "F2")},
               {"wallet": "B", "funded_by": "F2"},
               {"wallet": "C", "funder": "F1"}]
    assert find_clusters(wallets) == [["A", "B", "C"]]


def test_find_clusters_empty_and_none_input():
    assert find_clusters([]) == []
    assert find_clusters(None) == []


def test_find_clusters_ordering_is_size_then_first_member():
    wallets = [{"wallet": "M", "funder": "F1"}, {"wallet": "N", "funder": "F1"},
               {"wallet": "A", "funder": "F2"}, {"wallet": "B", "funder": "F2"},
               {"wallet": "Z", "funder": "F3"}]
    assert find_clusters(wallets) == [["A", "B"], ["M", "N"], ["Z"]]


def test_find_clusters_empty_funder_values_are_ignored():
    wallets = [{"wallet": "A", "funder": ""},
               {"wallet": "B", "funders": ["", None]}]
    assert find_clusters(wallets) == [["A"], ["B"]]


def test_find_clusters_five_wallet_transitivity_chain():
    wallets = [{"wallet": "W1", "funder": "F1"},
               {"wallet": "W2", "funders": ["F1", "F2"]},
               {"wallet": "W3", "funders": ["F2", "F3"]},
               {"wallet": "W4", "funders": ["F3", "F4"]},
               {"wallet": "W5", "funder": "F4"}]
    assert find_clusters(wallets) == [["W1", "W2", "W3", "W4", "W5"]]


# -- cluster_share edges ---------------------------------------------------------


def test_cluster_share_empty_cluster_is_zero_not_none():
    assert cluster_share([], {"A": 5}, SUPPLY) == 0.0
    assert cluster_share(["A"], None, SUPPLY) == 0.0


def test_cluster_share_string_values_and_junk_balances():
    balances = {"A": "50000", "B": "junk", "C": None}
    assert cluster_share(["A", "B", "C"], balances, "1000000") == 5.0


def test_cluster_share_nonfinite_or_negative_supply_is_none():
    for bad in (-5, float("inf"), float("nan"), "nope"):
        assert cluster_share(["A"], {"A": 1}, bad) is None


# -- classify_behavior edges -------------------------------------------------------


def test_behavior_two_of_four_distributing_but_two_of_five_holding():
    four = {"A": {"in": 0, "out": 5}, "B": {"in": 0, "out": 5},
            "C": {"in": 5, "out": 0}, "D": {"in": 5, "out": 0}}
    assert classify_behavior(four) == "distributing"  # 2*2 >= 4
    five = dict(four, E={"in": 5, "out": 0})
    assert classify_behavior(five) == "holding"       # 2*2 < 5


def test_behavior_equal_flows_not_distributing_outflow_only_is():
    assert classify_behavior({"A": {"in": 5, "out": 5}}) == "holding"
    assert classify_behavior({"A": {"in": 0, "out": 7}}) == "distributing"
    assert classify_behavior({"A": {"in": 7, "out": 0}}) == "holding"


def test_behavior_zero_activity_wallets_leave_the_denominator():
    flows = {"A": {"in": 0, "out": 9},
             "B": {"in": 0, "out": 0}, "C": {"in": 0, "out": 0},
             "D": {"in": 0, "out": 0}}
    # Only A is active, and A distributes: 1*2 >= 1.
    assert classify_behavior(flows) == "distributing"


def test_behavior_malformed_records_and_wrong_type_flows():
    assert classify_behavior("not-flows") == "unknown"
    assert classify_behavior(42) == "unknown"
    assert classify_behavior(["junk", {"in": "x", "out": "y"}]) == "unknown"
    mixed = ["junk", {"wallet": "A", "in": 0, "out": 3}]
    assert classify_behavior(mixed) == "distributing"  # junk skipped


def test_behavior_singular_inflow_outflow_key_names():
    flows = {"A": {"inflow": 1, "outflow": 9}}
    assert classify_behavior(flows) == "distributing"


# -- verdict boundaries -------------------------------------------------------------


def test_verdict_just_below_extreme_and_exactly_elevated_holding_flag():
    assert verdict_for_share(49.999, "holding")[0] == "FLAG"
    assert verdict_for_share(49.999, "unknown")[0] == "FLAG"
    assert verdict_for_share(20.0, "holding")[0] == "FLAG"
    assert verdict_for_share(0.001, "distributing")[0] == "PASS"


# -- evaluate scoping ------------------------------------------------------------------


def test_evaluate_scopes_dict_flows_to_the_winning_cluster():
    wallets = [{"wallet": "A", "funder": "F1"}, {"wallet": "B", "funder": "F1"},
               {"wallet": "C", "funder": "F2"}]
    balances = {"A": 150_000, "B": 100_000, "C": 10_000}  # A+B = 25%
    # C is distributing on its own; A holds, B is inactive. If C's
    # flow leaked into the big cluster it would read "distributing".
    flows = {"A": {"in": 10, "out": 1}, "B": {"in": 0, "out": 0},
             "C": {"in": 0, "out": 99}}
    result = evaluate(wallets, balances, SUPPLY, flows)
    assert result.cluster == ["A", "B"]
    assert result.behavior == "holding"
    assert result.verdict == "FLAG"


def test_evaluate_scopes_list_form_flows_to_the_winning_cluster():
    wallets = [{"wallet": "A", "funder": "F1"}, {"wallet": "B", "funder": "F1"},
               {"wallet": "C", "funder": "F2"}]
    balances = {"A": 150_000, "B": 100_000, "C": 10_000}
    flows = [{"wallet": "A", "in": 0, "out": 5},
             {"wallet": "B", "in": 0, "out": 5},
             {"wallet": "C", "in": 9, "out": 0}]
    result = evaluate(wallets, balances, SUPPLY, flows)
    assert result.cluster == ["A", "B"]
    assert result.behavior == "distributing"
    assert result.verdict == "VETO"


def test_evaluate_empty_cluster_and_tied_shares():
    empty = evaluate_cluster([], {"A": 1}, SUPPLY)
    assert empty.share_pct == 0.0
    assert empty.verdict == "PASS"
    wallets = [{"wallet": "B", "funder": "F2"}, {"wallet": "A", "funder": "F1"}]
    tied = evaluate(wallets, {"A": 100_000, "B": 100_000}, SUPPLY)
    assert tied.cluster == ["A"]  # first cluster wins a share tie
    assert tied.share_pct == 10.0


# -- cluster_shares / summarize_clusters / worst_verdict --------------------------------


def test_cluster_shares_sorted_descending_and_none_sorts_last():
    clusters = [["C"], ["A", "B"]]
    balances = {"A": 100_000, "B": 50_000, "C": 400_000}
    assert cluster_shares(clusters, balances, SUPPLY) == [
        (["C"], 40.0), (["A", "B"], 15.0)]
    bad = cluster_shares(clusters, balances, 0)
    assert bad == [(["A", "B"], None), (["C"], None)]  # ties by first member


def test_summarize_clusters_covers_every_cluster_sorted_by_share():
    wallets = [{"wallet": "A", "funder": "F1"}, {"wallet": "B", "funder": "F1"},
               {"wallet": "C", "funder": "F2"}, {"wallet": "D", "funder": "F2"},
               {"wallet": "E", "funder": "F3"}]
    balances = {"A": 200_000, "B": 100_000,   # 30% cluster, holding
                "C": 150_000, "D": 100_000,   # 25% cluster, distributing
                "E": 10_000}                  # 1% singleton
    flows = {"A": {"in": 9, "out": 1}, "B": {"in": 9, "out": 1},
             "C": {"in": 0, "out": 9}, "D": {"in": 0, "out": 9},
             "E": {"in": 0, "out": 9}}
    summary = summarize_clusters(wallets, balances, SUPPLY, flows)
    assert [v.cluster for v in summary] == [["A", "B"], ["C", "D"], ["E"]]
    assert [v.verdict for v in summary] == ["FLAG", "VETO", "PASS"]
    assert worst_verdict(summary) == "VETO"


def test_summarize_clusters_no_wallets_and_bad_supply():
    assert summarize_clusters([], {}, SUPPLY) == []
    summary = summarize_clusters([{"wallet": "A", "funder": "F"}],
                                 {"A": 1}, 0)
    assert len(summary) == 1
    assert summary[0].verdict == "UNKNOWN"
    assert summary[0].share_pct is None


def test_worst_verdict_precedence_and_fail_closed():
    assert worst_verdict(["PASS"]) == "PASS"
    assert worst_verdict(["PASS", "FLAG"]) == "FLAG"
    assert worst_verdict(["FLAG", "UNKNOWN"]) == "UNKNOWN"
    assert worst_verdict(["UNKNOWN", "VETO", "FLAG"]) == "VETO"
    assert worst_verdict([]) == "UNKNOWN"
    assert worst_verdict(None) == "UNKNOWN"
    assert worst_verdict(["PASS", "NOT-A-VERDICT"]) == "UNKNOWN"
