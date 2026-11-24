"""Component-flag token safety veto engine (synthetic lab).

Pure stdlib, no network.  Design rule, stated plainly: the score is a
*floor*, never a *ranker*.  A token either clears every hard rule or it
does not pass at all; a high score never outranks a single hard FAIL,
and tokens are never sorted by score to pick "the best of the risky".

Inputs are synthetic TokenReport fixtures describing a mint's
authorities and Token-2022 extensions, the way a safety API report
would.  Evaluation is component-based: each rule contributes a named
fail or flag, so a verdict can always be explained line by line.

Fail-closed on unknowns: if the safety data itself is missing (report
is None, a required field carries the "unknown" sentinel, the extension
list or the transfer fee was never fetched), the verdict is UNKNOWN
with passed=False.  Unknown is never quietly treated as clean.
"""
from __future__ import annotations

from dataclasses import dataclass, field

UNKNOWN = "unknown"
REVOKED_VALUES = {None, "", "revoked", "none", "burned"}

# Extension names are matched case-insensitively, without separators.
_HARD_EXTENSIONS = {
    "permanentdelegate": "permanent_delegate",
    "transferhook": "transfer_hook",
    "nontransferable": "non_transferable",
}
_FLAG_EXTENSIONS = {
    "mintcloseauthority": "mint_close_authority",
    "confidentialtransfermint": "confidential_transfer_mint",
}
MAX_TRANSFER_FEE_BPS = 1000  # >10% transfer fee is a hard FAIL


def _norm(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


@dataclass
class TokenReport:
    """One token's safety-relevant facts. All fixtures are synthetic.

    Authority fields: None / "revoked" means the authority is gone
    (clean); any other string is treated as a wallet that still holds
    it; the string "unknown" means the data was never fetched.
    ``extensions=None`` or ``transfer_fee_bps=None`` likewise mean
    "not fetched", which fails closed.
    """

    mint_authority: str | None = None
    freeze_authority: str | None = None
    extensions: list | None = field(default_factory=list)
    transfer_fee_bps: int | None = 0
    default_account_state: str | None = None  # e.g. "initialized"/"frozen"
    creator: str | None = None
    source: str = "synthetic-fixture"

    @classmethod
    def from_dict(cls, data: dict) -> "TokenReport":
        """Build from a dict; absent keys become the unknown sentinel."""
        if not isinstance(data, dict):
            raise ValueError("report dict required")
        return cls(
            mint_authority=data.get("mint_authority", UNKNOWN),
            freeze_authority=data.get("freeze_authority", UNKNOWN),
            extensions=data.get("extensions"),          # absent -> None
            transfer_fee_bps=data.get("transfer_fee_bps"),  # absent -> None
            default_account_state=data.get("default_account_state"),
            creator=data.get("creator"),
            source=data.get("source", "synthetic-dict"),
        )


@dataclass(frozen=True)
class Verdict:
    passed: bool
    fails: list
    flags: list
    score: int
    source: str
    status: str = "FAIL"  # "PASS" | "FAIL" | "UNKNOWN"


def _extension_names(extensions: list) -> tuple[set, str | None]:
    """Normalize extension entries to a name set + default state value.

    Entries may be plain names ("transferHook"), "name:value" strings
    ("defaultAccountState:frozen"), or dicts ({"type": ..., "state": ...}).
    """
    names: set = set()
    default_state: str | None = None
    for entry in extensions:
        if isinstance(entry, dict):
            raw = entry.get("type") or entry.get("name") or ""
            state = entry.get("state") or entry.get("value")
            if state is not None:
                default_state = str(state)
        else:
            raw = str(entry)
            if ":" in raw:
                raw, state = raw.split(":", 1)
                default_state = state
        name = _norm(raw)
        if name == "defaultaccountstate" and default_state is not None:
            # The state value, not the mere presence, is what matters.
            continue
        names.add(name)
    return names, default_state


def _authority_held(value) -> bool:
    return value is not None and _norm(value) not in {
        "", "revoked", "none", "burned"}


def _is_unknown(value) -> bool:
    return isinstance(value, str) and _norm(value) == UNKNOWN


def evaluate(report) -> Verdict:
    """Evaluate a TokenReport (or dict) -> Verdict. Never raises on
    missing safety data: missing data *is* the UNKNOWN verdict."""
    if report is None:
        return Verdict(False, ["unknown_report"], [], 0, "unknown", "UNKNOWN")
    if isinstance(report, dict):
        report = TokenReport.from_dict(report)
    if not isinstance(report, TokenReport):
        return Verdict(False, ["unknown_report_type"], [], 0,
                       "unknown", "UNKNOWN")
    source = report.source or "unknown"

    unknowns = []
    if _is_unknown(report.mint_authority):
        unknowns.append("mint_authority_unknown")
    if _is_unknown(report.freeze_authority):
        unknowns.append("freeze_authority_unknown")
    if report.extensions is None:
        unknowns.append("extensions_unknown")
    if report.transfer_fee_bps is None:
        unknowns.append("transfer_fee_unknown")
    if _is_unknown(report.default_account_state):
        unknowns.append("default_account_state_unknown")
    if unknowns:
        return Verdict(False, unknowns, [], 0, source, "UNKNOWN")

    fails: list = []
    flags: list = []

    if _authority_held(report.mint_authority):
        fails.append("mint_authority_held")
    if _authority_held(report.freeze_authority):
        fails.append("freeze_authority_held")

    ext_names, ext_default_state = _extension_names(report.extensions)
    for name, fail_name in _HARD_EXTENSIONS.items():
        if name in ext_names:
            fails.append(fail_name)
    for name, flag_name in _FLAG_EXTENSIONS.items():
        if name in ext_names:
            flags.append(flag_name)

    default_state = report.default_account_state or ext_default_state
    if default_state is not None and _norm(default_state) == "frozen":
        fails.append("default_account_state_frozen")

    fee = report.transfer_fee_bps
    # bool is a subclass of int, but True is not a fee: reject it too.
    if isinstance(fee, bool) or not isinstance(fee, int) or fee < 0:
        return Verdict(False, ["transfer_fee_invalid"], [], 0, source, "UNKNOWN")
    if fee > MAX_TRANSFER_FEE_BPS:
        fails.append("transfer_fee_excessive")
    elif fee > 0:
        flags.append("transfer_fee_present")

    score = max(0, 100 - 25 * len(fails) - 5 * len(flags))
    passed = not fails
    return Verdict(passed, fails, flags, score, source,
                   "PASS" if passed else "FAIL")


# ---------------------------------------------------------------------------
# Floor semantics + batch helpers
# ---------------------------------------------------------------------------

def meets_score_floor(verdict: Verdict, min_score: int) -> bool:
    """True iff the verdict passed *and* its score clears the floor.

    This is the only sanctioned use of the score: a minimum bar on top
    of the pass/fail verdict.  A failed verdict never meets any floor,
    not even 0 -- the floor can only tighten a PASS, never loosen a
    FAIL into a pass.
    """
    if isinstance(min_score, bool) or not isinstance(min_score, int) \
            or not (0 <= min_score <= 100):
        raise ValueError(f"min_score must be an int in [0, 100], "
                         f"got {min_score!r}")
    return bool(verdict.passed and verdict.score >= min_score)


def evaluate_many(reports) -> list:
    """Evaluate a batch of reports, verdicts in the same order."""
    if reports is None:
        raise ValueError("reports must be a sequence, got None")
    return [evaluate(report) for report in reports]


def summarize(verdicts) -> dict:
    """Status counts for a batch of verdicts (e.g. from evaluate_many)."""
    verdicts = list(verdicts)
    return {
        "total": len(verdicts),
        "passed": sum(1 for v in verdicts if v.passed),
        "pass": sum(1 for v in verdicts if v.status == "PASS"),
        "fail": sum(1 for v in verdicts if v.status == "FAIL"),
        "unknown": sum(1 for v in verdicts if v.status == "UNKNOWN"),
    }


def explain(verdict: Verdict) -> str:
    """A verdict as plain lines: header, then one line per fail/flag."""
    lines = [f"{verdict.status} score={verdict.score} "
             f"source={verdict.source}"]
    lines += [f"fail: {name}" for name in verdict.fails]
    lines += [f"flag: {name}" for name in verdict.flags]
    if not verdict.fails and not verdict.flags:
        lines.append("no fails or flags")
    return "\n".join(lines)
