#!/usr/bin/env python3
"""Two-phase pre-buy check pipeline (2026-11-17).

Adapted from the pumpfun-bonkfun-bot reference
(src/trading/checks/pipeline.py, base.py — read fully): one class
per gate, check() -> str | None (None = pass, string = skip
reason); split by reads_chain; first skip wins; per-check budget;
required vs best-effort on unreadable data.

Differences from the reference, all deliberate:
- Sync, not async: this bot's entry path is sync. The per-check
  budget is enforced by running the check in a daemon thread and
  joining with a timeout — a hung check (e.g. an RPC that never
  returns) cannot stall the entry loop past its budget. The
  abandoned thread is a daemon: it dies with the process, and its
  result is discarded. A check that returns AFTER its budget is
  still a budget failure, exactly like the reference.
- Exception text is NEVER logged and never surfaces in a reason:
  a check that raises reports only its name + the failure kind
  (timeout / unavailable / bad-return / <ExceptionClass>).
  Reasons stay stable, journalable keys.
- ``named=False`` keeps the check's own reason verbatim (the
  grad_select filters' reasons already name the gate, e.g.
  "serial_deployer"); ``named=True`` prefixes "<name>: <reason>".

Phases (the point of the split): free checks (reads_chain=False)
run at detection and never wait on RPC; chain checks
(reads_chain=True) run just before the buy, after any creation
wait. A check that reads no chain itself but depends on
chain-produced values (or must only see filter-passing
candidates) is ordered in the chain phase and says so in its
docstring. ``run_phases`` runs free first, then chain — the
first skip across both phases wins.

``required`` (default True) decides what happens when a check
cannot decide (raises CheckUnavailable, times out, or returns
something other than None or a str): required refuses the coin
(fail-closed), best-effort lets it through and logs the skip.
This formalises the fail-closed/fail-open policy the bot already
lives by, so a new check cannot silently change it.

Pure stdlib. No network, no disk, no state beyond the caller's
context object (passed through opaquely). Never raises.
"""

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Seconds a check may take when its entry sets no budget.
DEFAULT_BUDGET = 2.0


class CheckUnavailable(Exception):
    """A check could not read what it needs, so it cannot decide.

    Any other exception from a check counts the same; only the
    failure KIND is ever reported, never the exception text.
    """


class PreBuyCheck:
    """One check a coin must pass before the bot buys it.

    Subclass and implement check(); set the class attributes.
    ``reads_chain`` True means the check runs just before the buy,
    after any creation wait; False means it runs as soon as the
    coin is detected. ``required_by_default`` is the ``required``
    value when the ConfiguredCheck entry leaves it out.
    """

    name = "unnamed"
    reads_chain = False
    required_by_default = True
    #: Reason returned when this check cannot decide (raises,
    #: times out, bad return). None falls back to
    #: "unknown_<name>". Set it to the check's existing stable
    #: unknown_* key so a failure journals exactly what the
    #: inline filter journaled before the pipeline existed.
    unavailable_reason = None

    def check(self, cand, ctx):
        """None to pass the coin, or why it is skipped.

        Raises:
            CheckUnavailable: the data the check needs could not
                be read; any other exception counts the same.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class ConfiguredCheck:
    """A check as configured.

    Args:
        required: when the check can't decide (it raised, ran
            past its budget, or returned something other than
            None or a str), True refuses the coin and False lets
            it through.
        budget: seconds the check may take; None is no limit.
        named: prefix its reason with the check's name; off for
            checks whose reasons name the gate already.
    """

    check: PreBuyCheck
    required: bool = True
    budget: float | None = DEFAULT_BUDGET
    named: bool = True


class CheckPipeline:
    """The configured checks, run in order; the first skip wins."""

    def __init__(self, checks, ctx=None):
        self.checks = list(checks)
        self.ctx = ctx

    def has(self, *, reads_chain):
        """Whether any check of that kind is configured."""
        return any(c.check.reads_chain is reads_chain
                   for c in self.checks)

    def reason(self, cand, *, reads_chain):
        """Why a check refuses the coin, or None.

        Runs only the checks whose ``reads_chain`` matches, in
        configured order. The first skip wins. Never raises.
        """
        for configured in self.checks:
            if configured.check.reads_chain is not reads_chain:
                continue
            reason = self._run(configured, cand)
            if reason is not None:
                if configured.named:
                    return "%s: %s" % (configured.check.name, reason)
                return reason
        return None

    def run_phases(self, cand):
        """Free phase first, then chain phase; first skip wins.

        This is what the entry path calls: the two phases in
        order, with the same first-skip-wins semantics as a
        single ordered run. Never raises."""
        reason = self.reason(cand, reads_chain=False)
        if reason is not None:
            return reason
        return self.reason(cand, reads_chain=True)

    def _run(self, configured, cand):
        """Run one configured check inside its budget.

        Returns the skip reason or None. A check that cannot
        decide yields a failure: required refuses with a stable
        reason, best-effort proceeds (logged, kind only).
        Never raises; never logs exception text."""
        check = configured.check
        budget = configured.budget
        holder = {}
        done = threading.Event()

        def _target():
            try:
                holder["result"] = check.check(cand, self.ctx)
            except CheckUnavailable:
                holder["failure"] = "unavailable"
            except Exception as e:  # noqa: BLE001 - a check must
                # never stop the trader; only the kind is kept.
                holder["failure"] = type(e).__name__
            finally:
                done.set()

        thread = threading.Thread(target=_target, daemon=True)
        started = time.monotonic()
        try:
            thread.start()
        except Exception as e:  # noqa: BLE001 - thread spawn is
            # the one failure that cannot be budgeted.
            return self._failure(configured,
                                 type(e).__name__)
        if budget is None:
            done.wait()
        else:
            done.wait(timeout=budget)
        if not done.is_set():
            # Still running past its budget: abandon the thread
            # (daemon) and treat it as a failure.
            return self._failure(
                configured,
                "ran past its %ss budget" % (budget,))
        elapsed = time.monotonic() - started
        if budget is not None and elapsed > budget:
            # It answered, but after its budget.
            return self._failure(
                configured,
                "ran past its %ss budget" % (budget,))
        if "failure" in holder:
            return self._failure(configured, holder["failure"])
        result = holder.get("result")
        if result is None or isinstance(result, str):
            return result
        return self._failure(
            configured,
            "returned %s, not None or a reason"
            % type(result).__name__)

    def _failure(self, configured, kind):
        """A check that could not decide: refuse or proceed.

        ``kind`` is a failure KIND only — never exception text.
        Required refuses with a stable reason; best-effort logs
        and lets the coin through."""
        check = configured.check
        if configured.required:
            logger.warning("check_pipeline: %s could not decide "
                           "(%s) — refusing (required)",
                           check.name, kind)
            return check.unavailable_reason or \
                "unknown_%s" % (check.name,)
        logger.warning("check_pipeline: %s could not decide (%s) "
                       "— proceeding (best-effort)",
                       check.name, kind)
        return None
