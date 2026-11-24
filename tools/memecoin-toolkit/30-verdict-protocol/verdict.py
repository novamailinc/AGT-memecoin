"""Lane-verdict protocol lab (synthetic, pure stdlib).

Decision matrix over a lane record + the four live gates. Verdicts:
FUND-ELIGIBLE / KEEP-FIXING / KEEP-MEASURING / KILL. Every verdict renders
with reasons. Fail-closed: malformed lanes raise; insufficient sample
refuses a definitive verdict (KEEP-MEASURING).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# Distinguishing WR 35% from 30% at alpha 0.05 one-sided, power 0.8 -> ~534
# (spirit of toolkit 08; full FUND verdicts need this; kill/fix need less).
REQUIRED_N_FULL = 534
MIN_N_ANY_VERDICT = 20       # below this: KEEP-MEASURING (the structural-kill rule is checked before this gate and fires at any sample)
MIN_N_KILL = 50              # deep-negative kill needs at least this many closes
DEEP_NEGATIVE_EXPECTANCY = -0.20  # per $1

VERDICTS = ("FUND-ELIGIBLE", "KEEP-FIXING", "KEEP-MEASURING", "KILL")
GATE_NAMES = (
    "sustained_positive_expectancy",
    "proven_entry_strategy",
    "top_picks_winning",
    "hardened_execution_safety",
)


def required_n(p1: float = 0.35, p0: float = 0.30, alpha: float = 0.05, power: float = 0.8) -> int:
    """Normal-approx one-sided sample size (mirrors toolkit 08's formula)."""
    for name, v in (("p1", p1), ("p0", p0), ("alpha", alpha), ("power", power)):
        if v is None or not (0 < v < 1):
            raise ValueError(f"{name} must be in (0, 1), got {v!r}")
    if p1 <= p0:
        raise ValueError("p1 must exceed p0")
    # inverse normal CDF (Acklam approximation, stdlib-only)
    def z(q):
        a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285984469534e+02,
             1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
        b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
             6.680131188771972e+01, -1.328068155288572e+01]
        c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
             -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
        d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
             3.754408661907416e+00]
        plow, phigh = 0.02425, 1 - 0.02425
        if q < plow:
            r = math.sqrt(-2 * math.log(q))
            return (((((c[0]*r+c[1])*r+c[2])*r+c[3])*r+c[4])*r+c[5]) / ((((d[0]*r+d[1])*r+d[2])*r+d[3])*r+1)
        if q > phigh:
            r = math.sqrt(-2 * math.log(1 - q))
            return -(((((c[0]*r+c[1])*r+c[2])*r+c[3])*r+c[4])*r+c[5]) / ((((d[0]*r+d[1])*r+d[2])*r+d[3])*r+1)
        r = q - 0.5
        s = r * r
        return (((((a[0]*s+a[1])*s+a[2])*s+a[3])*s+a[4])*s+a[5]) * r / (((((b[0]*s+b[1])*s+b[2])*s+b[3])*s+b[4])*s+1)
    za = z(1 - alpha)
    zb = z(power)
    num = (za * math.sqrt(p0 * (1 - p0)) + zb * math.sqrt(p1 * (1 - p1))) ** 2
    return math.ceil(num / (p1 - p0) ** 2)


@dataclass
class Gates:
    sustained_positive_expectancy: bool = False
    proven_entry_strategy: bool = False
    top_picks_winning: bool = False
    hardened_execution_safety: bool = False

    def as_dict(self) -> dict:
        return {name: bool(getattr(self, name)) for name in GATE_NAMES}

    def all_green(self) -> bool:
        return all(self.as_dict().values())

    def red_gates(self) -> list:
        return [k for k, v in self.as_dict().items() if not v]


@dataclass
class LaneRecord:
    name: str
    closes: int
    expectancy: float  # per $1
    win_rate: float    # 0..1
    weeks_of_data: float
    structural_dead: bool = False

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("name must be non-empty string")
        if self.closes is None or not isinstance(self.closes, int) or isinstance(self.closes, bool) or self.closes < 0:
            raise ValueError(f"closes must be a non-negative int, got {self.closes!r}")
        if self.expectancy is None or not isinstance(self.expectancy, (int, float)) or isinstance(self.expectancy, bool):
            raise ValueError("expectancy must be a number")
        if self.win_rate is None or not (0 <= self.win_rate <= 1):
            raise ValueError(f"win_rate must be in [0, 1], got {self.win_rate!r}")
        if self.weeks_of_data is None or self.weeks_of_data < 0:
            raise ValueError("weeks_of_data must be >= 0")
        if not isinstance(self.structural_dead, bool):
            raise ValueError("structural_dead must be bool")


@dataclass
class Verdict:
    decision: str
    lane: str
    reasons: list = field(default_factory=list)

    def render(self) -> str:
        lines = [f"{self.lane}: {self.decision}"]
        for r in self.reasons:
            lines.append(f"  - {r}")
        return "\n".join(lines)

    def __str__(self):
        return self.render()


def sample_sufficient(closes: int, level: str = "full") -> bool:
    if closes is None or closes < 0:
        raise ValueError("closes must be >= 0")
    if level == "full":
        return closes >= REQUIRED_N_FULL
    if level == "any":
        return closes >= MIN_N_ANY_VERDICT
    if level == "kill":
        return closes >= MIN_N_KILL
    raise ValueError(f"unknown level: {level!r}")


def decide(lane: LaneRecord, gates: Gates) -> Verdict:
    if not isinstance(lane, LaneRecord):
        raise ValueError("expected LaneRecord")
    if not isinstance(gates, Gates):
        raise ValueError("expected Gates")
    reasons = []
    # 1. Structural kill: window proven dead -> KILL regardless of sample/gates.
    if lane.structural_dead:
        reasons.append("structural kill: the lane's trading window is proven dead")
        reasons.append(f"closes={lane.closes} expectancy={lane.expectancy:+.3f} win_rate={lane.win_rate:.2f}")
        return Verdict("KILL", lane.name, reasons)
    # 2. Insufficient sample for any verdict -> refuse, keep measuring.
    if lane.closes < MIN_N_ANY_VERDICT:
        reasons.append(f"sample too small: {lane.closes} closes < {MIN_N_ANY_VERDICT} minimum — verdict refused")
        reasons.append(f"full verdict needs {REQUIRED_N_FULL} closes (35% vs 30% WR separation)")
        return Verdict("KEEP-MEASURING", lane.name, reasons)
    # 3. Deep negative expectancy at sufficient kill sample -> KILL/halt.
    if lane.expectancy <= DEEP_NEGATIVE_EXPECTANCY and lane.closes >= MIN_N_KILL:
        reasons.append(f"deep negative expectancy {lane.expectancy:+.3f} per $1 at {lane.closes} closes (>= {MIN_N_KILL})")
        reasons.append("halt: continued measuring spends attention, not information")
        return Verdict("KILL", lane.name, reasons)
    # 4. Negative expectancy but kill sample not yet met -> keep measuring.
    if lane.expectancy < 0 and lane.closes < REQUIRED_N_FULL:
        reasons.append(f"negative expectancy {lane.expectancy:+.3f} but only {lane.closes} closes (< {REQUIRED_N_FULL} full / kill needs {MIN_N_KILL} at deep-negative)")
        if lane.closes < MIN_N_KILL:
            reasons.append("below kill sample — keep measuring before any kill call")
            return Verdict("KEEP-MEASURING", lane.name, reasons)
        reasons.append("negative but not deep-negative — fix entries/exits, re-measure")
        return Verdict("KEEP-FIXING", lane.name, reasons)
    # 5. Fund-eligible: ALL gates green AND full sample AND positive expectancy.
    if gates.all_green():
        if lane.closes >= REQUIRED_N_FULL and lane.expectancy > 0:
            reasons.append("all four live gates green")
            reasons.append(f"sample sufficient: {lane.closes} closes >= {REQUIRED_N_FULL}")
            reasons.append(f"expectancy {lane.expectancy:+.3f} per $1, win_rate {lane.win_rate:.2f}, {lane.weeks_of_data} weeks")
            return Verdict("FUND-ELIGIBLE", lane.name, reasons)
        reasons.append("gates green but sample/expectancy not yet sufficient for funding")
        if lane.closes < REQUIRED_N_FULL:
            reasons.append(f"closes {lane.closes} < {REQUIRED_N_FULL} required")
            return Verdict("KEEP-MEASURING", lane.name, reasons)
        return Verdict("KEEP-FIXING", lane.name, reasons)
    # 6. Otherwise: positive/neutral lane with red gates -> keep fixing.
    red = gates.red_gates()
    reasons.append(f"red gates: {', '.join(red)}")
    if lane.expectancy > 0:
        reasons.append(f"expectancy positive ({lane.expectancy:+.3f}) — fix the red gates, do not fund yet")
    else:
        reasons.append(f"expectancy {lane.expectancy:+.3f} — no funding case until gates and expectancy agree")
    return Verdict("KEEP-FIXING", lane.name, reasons)


# --- fixtures ---------------------------------------------------------------
def degen_v2_fixture() -> tuple:
    return (LaneRecord("degen_v2", closes=120, expectancy=-0.10, win_rate=0.28,
                       weeks_of_data=3.0, structural_dead=True), Gates())

def small_sample_fixture() -> tuple:
    return (LaneRecord("steady_new", closes=18, expectancy=0.05, win_rate=0.39,
                       weeks_of_data=1.0), Gates(True, False, False, False))

def halted_394_fixture() -> tuple:
    return (LaneRecord("main_lane", closes=394, expectancy=-0.375, win_rate=0.22,
                       weeks_of_data=8.0), Gates())

def all_green_fixture() -> tuple:
    return (LaneRecord("proven_lane", closes=600, expectancy=0.18, win_rate=0.41,
                       weeks_of_data=10.0),
            Gates(True, True, True, True))
