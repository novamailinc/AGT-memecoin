"""Narrative classification and outcome heat.

Classification matches names against a fixed keyword taxonomy. Heat compares
per-tag and global runner rates, with a supplied multiplier >=2 and rugFalse
counting as a runner. The scanner currently supplies its legacy realized pnl
normalized by configured entry size, not an observed peak multiplier. Shared
close recovery preserves that metric and pins source close time.

These values can affect decisions: sniper adds a bounded positive heat bonus
and winner_model buckets heat as an entry feature. This module itself does not
submit trades or bypass safety checks. Cold heat adds no sniper bonus.

State holds at most5000 outcomes; exact-effect receipts are retained separately
and currently unbounded. File access is local, with no network calls.
"""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

# -- taxonomy -----------------------------------------------------------
# Each tag: 6-15 lowercase keywords. Matching is substring-based, so
# keep keywords specific enough to avoid accidental hits.
NARRATIVE_KEYWORDS: Dict[str, List[str]] = {
    "dog": ["dog", "doge", "shiba", "inu", "puppy", "pup", "hound",
            "canine", "akita", "corgi", "bulldog", "pomeranian"],
    "cat": ["cat", "kitten", "kitty", "meow", "feline", "neko",
            "gato", "paws"],
    "frog": ["frog", "pepe", "toad", "ribbit", "tadpole",
             "bullfrog", "pond"],
    "ai": ["ai", "artificial", "gpt", "llm", "neural", "robot",
           "agent", "machine", "deep", "cortex", "synth", "cyber",
           "compute"],
    "gaming": ["game", "gaming", "gamer", "play", "quest", "arcade",
               "rpg", "esports", "metaverse", "pixel", "console",
               "speedrun"],
    "political": ["trump", "maga", "biden", "vote", "election",
                  "president", "senate", "congress", "democrat",
                  "republican", "politic", "freedom", "patriot"],
    "celebrity": ["elon", "musk", "celebrity", "celebr", "famous",
                  "influencer", "kardashian", "bieber", "taylor",
                  "ronaldo", "messi", "superstar"],
    "sports": ["sport", "football", "soccer", "basketball", "baseball",
               "tennis", "olympic", "champion", "league", "touchdown",
               "knockout", "derby"],
    "food": ["food", "pizza", "burger", "taco", "sushi", "ramen",
             "cookie", "cake", "chocolate", "coffee", "noodle",
             "snack"],
    "space": ["space", "mars", "lunar", "rocket", "orbit", "galaxy",
              "cosmos", "astronaut", "satellite", "nebula",
              "interstellar", "starship"],
    "degen": ["ape", "moon", "lambo", "hodl", "degen", "moonshot",
              "gem", "pump", "sendit", "wagmi", "diamond", "100x",
              "1000x"],
    "rwa": ["rwa", "realworld", "estate", "property", "treasury",
            "bond", "yield", "tokenized", "commodity", "gold",
            "silver", "reit"],
    "meme_classic": ["pepe", "doge", "shib", "bonk", "wif", "floki",
                     "brett", "popcat", "mog", "turbo", "neiro",
                     "ponke"],
}

OTHER = "other"

# -- heat tuning ----------------------------------------------------------
DAY_S = 86400.0
OUTCOME_TTL_S = 72 * 3600.0          # prune() drops outcomes older than this
MAX_OUTCOMES = 5000                  # hard bound; oldest evicted first
RUNNER_MULT = 2.0                    # peak multiple that counts as a runner
MIN_CLOSED_PER_TAG = 3               # min closed outcomes before a tag is trusted
HEAT_MIN = 0.5
HEAT_MAX = 2.0


def _s(value: Any) -> str:
    """Never-raising str()."""
    try:
        return str(value)
    except Exception:
        return ""


def classify_narrative(symbol: Any, name: Any,
                       description: Any = "") -> List[str]:
    """Tag a coin from its symbol/name/description.

    Case-insensitive substring match against ``NARRATIVE_KEYWORDS``.
    Returns the sorted unique matched tags, or ``["other"]`` when
    nothing matches. Never raises: ``None``/numbers/etc. are
    stringified first.
    """
    text = " ".join(_s(v) for v in (symbol, name, description)).lower()
    matched = [tag for tag, keywords in NARRATIVE_KEYWORDS.items()
               if any(kw in text for kw in keywords)]
    return sorted(set(matched)) or [OTHER]


class NarrativeHeat:
    """Heat multipliers per narrative, learned from closed outcomes.

    ``record_outcome`` feeds closed-runner results in; ``heat`` answers
    how hot a set of tags is right now, purely from recorded data.
    """

    def __init__(self) -> None:
        # (ts, tags tuple, peak mult, rug bool) — oldest on the left.
        self._outcomes: Deque[Tuple[float, Tuple[str, ...], float, bool]] = \
            deque(maxlen=MAX_OUTCOMES)
        self._pending = []
        self._prune_now = None

    # -- recording ------------------------------------------------------
    @staticmethod
    def _norm_tags(tags: Any) -> Tuple[str, ...]:
        if tags is None:
            return (OTHER,)
        if isinstance(tags, str):
            tags = [tags]
        try:
            items = list(tags)
        except TypeError:
            items = [tags]
        seen = set()
        out: List[str] = []
        for t in items:
            s = _s(t).strip().lower()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return tuple(sorted(out)) or (OTHER,)

    @staticmethod
    def _f(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def record_outcome(self, tags: Any, mult: Any, rug: Any,
                       now: Any) -> None:
        """Record a supplied outcome multiplier. Scanner metric is described above."""
        import uuid
        canonical=validate_heat_outcome([now,list(self._norm_tags(tags)),mult,rug])
        outcome=(canonical[0],tuple(canonical[1]),canonical[2],canonical[3])
        identity='narrative-'+uuid.uuid4().hex
        self._outcomes.append(outcome)
        self._pending.append((identity,canonical))

    # -- heat ------------------------------------------------------------
    def _recent(self, now: float, window_s: float = DAY_S) -> list:
        cutoff = self._f(now) - window_s
        return [o for o in self._outcomes if o[0] >= cutoff]

    @staticmethod
    def _is_runner(outcome: Tuple[float, Tuple[str, ...], float, bool]) -> bool:
        _ts, _tags, mult, rug = outcome
        return (not rug) and mult >= RUNNER_MULT

    def heat(self, tags: Any, now: Any) -> float:
        """Heat multiplier in [0.5, 2.0] for a set of tags.

        Per tag (last 24h): runner_rate = runners / closed; a tag needs
        at least ``MIN_CLOSED_PER_TAG`` closed outcomes to be trusted,
        otherwise it contributes 1.0. tag_heat = 1.0 +
        clamp(tag_runner_rate - global_runner_rate, -0.5, +1.0).
        The result is the average of the tag heats; ``["other"]`` and
        unknown tags are always neutral at 1.0.
        """
        norm = self._norm_tags(tags)
        if norm == (OTHER,):
            return 1.0
        now_f = self._f(now)
        recent = self._recent(now_f)
        if not recent:
            return 1.0
        global_rate = (sum(1 for o in recent if self._is_runner(o))
                       / len(recent))
        heats: List[float] = []
        for tag in norm:
            tagged = [o for o in recent if tag in o[1]]
            if len(tagged) < MIN_CLOSED_PER_TAG:
                heats.append(1.0)
                continue
            tag_rate = (sum(1 for o in tagged if self._is_runner(o))
                        / len(tagged))
            delta = max(-0.5, min(1.0, tag_rate - global_rate))
            heats.append(1.0 + delta)
        avg = sum(heats) / len(heats)
        return round(max(HEAT_MIN, min(HEAT_MAX, avg)), 3)

    def leaderboard(self, now: Any) -> List[Dict[str, Any]]:
        """Per-tag heat table, hottest first (read-only).

        One row per taxonomy tag with at least ``MIN_CLOSED_PER_TAG``
        closed outcomes in the last 24h:
        ``{"tag", "heat", "closed", "runners", "runner_rate"}``.
        Tags without enough data are omitted — never guessed.
        Powers the /heat leaderboard (#5).
        """
        now_f = self._f(now)
        recent = self._recent(now_f)
        if not recent:
            return []
        global_rate = (sum(1 for o in recent if self._is_runner(o))
                       / len(recent))
        rows: List[Dict[str, Any]] = []
        for tag in sorted(NARRATIVE_KEYWORDS):
            tagged = [o for o in recent if tag in o[1]]
            if len(tagged) < MIN_CLOSED_PER_TAG:
                continue
            n_run = sum(1 for o in tagged if self._is_runner(o))
            tag_rate = n_run / len(tagged)
            delta = max(-0.5, min(1.0, tag_rate - global_rate))
            rows.append({
                "tag": tag,
                "heat": round(max(HEAT_MIN, min(HEAT_MAX, 1.0 + delta)), 3),
                "closed": len(tagged),
                "runners": n_run,
                "runner_rate": round(tag_rate, 3),
            })
        rows.sort(key=lambda r: (-r["heat"], -r["closed"], r["tag"]))
        return rows

    # -- hygiene -----------------------------------------------------------
    def prune(self, now: Any) -> None:
        """Prune this tracker's view, not the shared store.

        Save refreshes respect this monotonic local watermark. Reload starts a
        new view; physical store retention follows latest recorded source time.
        """
        import math
        if type(now) not in (int,float) or not math.isfinite(now):
            raise ValueError('narrative prune time invalid')
        self._prune_now=max(now,self._prune_now) if self._prune_now is not None else now
        cutoff = self._prune_now - OUTCOME_TTL_S
        self._outcomes = deque(
            (o for o in self._outcomes if o[0] >= cutoff),
            maxlen=MAX_OUTCOMES)

    def __len__(self) -> int:
        return len(self._outcomes)

    # -- persistence ---------------------------------------------------------
    def to_state(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "outcomes": [[ts, list(tags), mult, rug]
                         for ts, tags, mult, rug in self._outcomes],
        }

    @classmethod
    def from_state(cls, state: Any) -> "NarrativeHeat":
        """Tolerant rebuild: malformed input yields an empty tracker."""
        inst = cls()
        if not isinstance(state, dict):
            return inst
        outcomes = state.get("outcomes")
        if not isinstance(outcomes, list):
            return inst
        for item in outcomes:
            try:
                ts, tags, mult, rug = item
            except (TypeError, ValueError):
                continue
            try:inst.record_outcome(tags, mult, rug, ts)
            except ValueError:continue
        inst._pending=[]
        return inst

    def save(self, path: Any) -> None:
        """Merge pending exact outcomes, never overwrite another writer's snapshot."""
        p=Path(_s(path)).absolute()
        for identity,outcome in self._pending:
            append_heat(p,outcome,identity)
        # Clear only after every exact append has confirmed durability. A fault
        # leaves all pending identities intact so an exact retry is safe.
        loaded=read_heat(p)
        self._outcomes=deque((tuple([o[0],tuple(o[1]),o[2],o[3]]) for o in loaded['outcomes']),maxlen=MAX_OUTCOMES)
        self._pending=[]
        if self._prune_now is not None:self.prune(self._prune_now)

    @classmethod
    def load(cls, path: Any) -> "NarrativeHeat":
        """Missing file starts empty. Corruption is unavailable, never overwritten."""
        return cls.from_state(read_heat(Path(_s(path)).absolute()))


def validate_heat(state):
    from . import learning_effects as effects
    effects._validate(state)
    if not isinstance(state,dict) or set(state)!={'version','outcomes'} or type(state['version']) is not int or state['version']!=1 or not isinstance(state['outcomes'],list):
        raise ValueError('narrative state unavailable')
    for outcome in state['outcomes']:validate_heat_outcome(outcome)
    return state


def validate_heat_outcome(outcome):
    import math
    if not isinstance(outcome,list) or len(outcome)!=4:raise ValueError('narrative outcome unavailable')
    ts,tags,mult,rug=outcome
    if (type(ts) not in (int,float) or not math.isfinite(ts) or type(mult) not in (int,float) or not math.isfinite(mult) or mult<0
            or not isinstance(tags,list) or not tags or any(not isinstance(t,str) or not t for t in tags)
            or tags!=sorted(set(tags)) or type(rug) is not bool):raise ValueError('narrative outcome invalid')
    return outcome


def read_heat(path):
    from . import learning_effects as effects
    try:doc=json.loads(Path(path).read_text(),object_pairs_hook=effects._pairs,parse_constant=effects._constant)
    except FileNotFoundError:return {'version':1,'outcomes':[]}
    if isinstance(doc,dict) and set(doc)=={'version','value','effects'}:
        if type(doc['version']) is not int or doc['version']!=1 or not isinstance(doc['effects'],dict) or any(not isinstance(k,str) or not k or not isinstance(v,str) or len(v)!=64 or any(c not in '0123456789abcdef' for c in v) for k,v in doc['effects'].items()):
            raise ValueError('narrative receipts unavailable')
        doc=doc['value']
    return validate_heat(doc)


def append_heat(path,outcome,effect_id):
    from . import learning_effects as effects
    p=Path(path)
    if not p.is_absolute():raise ValueError('narrative path must be absolute')
    validate_heat_outcome(outcome)
    p.parent.mkdir(parents=True,exist_ok=True)
    def reducer(state,payload):
        validate_heat(state);validate_heat_outcome(payload)
        rows=state['outcomes']+[payload]
        cutoff=max(o[0] for o in rows)-OUTCOME_TTL_S
        rows=[o for o in rows if o[0]>=cutoff]
        rows.sort(key=lambda o:(o[0],json.dumps(o,sort_keys=True,separators=(',',':'))))
        return validate_heat({'version':1,'outcomes':rows[-MAX_OUTCOMES:]})
    return effects.apply(p,effect_id,outcome,reducer,{'version':1,'outcomes':[]},legacy_import=validate_heat)


def default_heat_path() -> Path:
    """~/.forex-agent/launch_scanner/narrative_heat.json.

    Honors FOREX_AGENT_HOME when set (test/dev convention).
    """
    base = os.environ.get("FOREX_AGENT_HOME") or str(Path.home() / ".forex-agent")
    return Path(base) / "launch_scanner" / "narrative_heat.json"
