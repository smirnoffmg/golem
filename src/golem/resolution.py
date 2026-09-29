"""A person's answer to a process waiting for a reason (ADR 0019), and the reasons a rejection
carries: one definition for the board's backend, the edge, the task service and the reconciler.
"""

RERUN = "rerun"
END = "end"
ACTIONS = frozenset({RERUN, END})
MAX_REASON_CHARS = 4000
