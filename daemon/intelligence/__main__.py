"""`python3 -m daemon.intelligence`: the intelligence runtime daemon.

NOT in any supervisor roster and not started by anything in the repo: a
person has to run it, or add it to the roster in a separate reviewed
change. With no INTELLIGENCE_STREAMS configured it runs idle and reports
state "unconfigured" (never "ok"). Shadow only; no trading path.
"""
import logging
import sys

from daemon.common import Daemon, get_home, main

from daemon.intelligence import runtime as _runtime

logger = logging.getLogger("forex_agent.daemon.intelligence")


class IntelligenceDaemon(Daemon):
    name = "intelligence"
    interval = 30.0

    def __init__(self, interval=None, runtime=None):
        Daemon.__init__(self, interval=interval)
        self._runtime = runtime

    def run_once(self):
        if self._runtime is None:
            self._runtime = _runtime.build_runtime(get_home())
        status = self._runtime.run_cycle()
        if status["state"] != _runtime.STATE_OK:
            logger.warning("intelligence runtime state=%s", status["state"])

    def run_forever(self):
        try:
            return Daemon.run_forever(self)
        finally:
            rt = self._runtime
            if rt is not None:
                try:
                    rt.state.save()
                    rt.store.close()
                except Exception:
                    logger.exception("intelligence runtime close failed")


if __name__ == "__main__":
    sys.exit(main(IntelligenceDaemon, sys.argv[1:]))
