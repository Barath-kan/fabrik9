"""Structured logging for the FABRIK-9 backend.

One idempotent entry point — `configure_logging()` — installs a structured
formatter on the ``fabrik9`` logger namespace. Every module logs through
``get_logger("<area>")``; lifecycle events attach machine-parseable context
via logging's ``extra=`` dict, which the formatter renders as ``key=value``
pairs after the message. That gives greppable, structured output
(``grep 'fault injected' | grep fault_x=17``) without pulling in a JSON-logging
dependency.

Deliberately *not* configured at import time: until `configure_logging()` runs
(from the app lifespan), the ``fabrik9`` logger has no handler and propagates to
root at WARNING, so importing the sim in a test or script stays quiet. Set
``FABRIK_LOG_LEVEL=DEBUG`` to surface the per-tick auction trace.
"""

import logging
import os
import sys

LOGGER_NAME = "fabrik9"

# Standard LogRecord attributes — everything else on a record was supplied by a
# caller via ``extra=`` and should be rendered as structured context.
_STD_ATTRS = frozenset((
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
))


def _fmt_value(v):
    s = str(v)
    return f'"{s}"' if (" " in s or not s) else s


class StructuredFormatter(logging.Formatter):
    """Renders ``ts level logger message key=value ...`` — human-scannable and
    grep-friendly. Structured fields come from the record's ``extra`` dict."""

    def format(self, record):
        base = super().format(record)
        extras = {k: v for k, v in record.__dict__.items()
                  if k not in _STD_ATTRS and not k.startswith("_")}
        if extras:
            kv = " ".join(f"{k}={_fmt_value(v)}" for k, v in extras.items())
            return f"{base} {kv}"
        return base


_configured = False


def configure_logging(level=None, stream=None):
    """Install the structured handler on the ``fabrik9`` logger. Idempotent:
    safe to call from every process/worker startup."""
    global _configured
    if _configured:
        return
    level = (level or os.environ.get("FABRIK_LOG_LEVEL", "INFO")).upper()
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(StructuredFormatter(
        "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S"))
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    _configured = True


def get_logger(area):
    """Return the ``fabrik9.<area>`` logger (e.g. ``get_logger("runtime")``)."""
    return logging.getLogger(f"{LOGGER_NAME}.{area}")


# Library-quiet-by-default: a NullHandler on the root ``fabrik9`` logger means
# that until an entry point calls `configure_logging()`, records are swallowed
# instead of falling through to logging's last-resort stderr handler. This keeps
# pure-sim harnesses (regress.py) and the test suite silent; `configure_logging`
# clears this and installs the real handler.
logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())
