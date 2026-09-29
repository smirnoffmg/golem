"""What the board's backend, the edge and the task service all check of a request about
proposals and reports (ADR 0015, ADR 0018), so that they never disagree on what is well formed."""

import re

# The agents whose catalogs name the principal a reviewer, set by the edge alone.
REVIEWS_HEADER = "x-golem-reviews"
STATES = frozenset({"pending", "accepted", "applied", "rejected", "stale", "failed"})
DECISIONS = frozenset({"accept", "reject"})
NAME = re.compile(r"^[a-z][a-z0-9-]*$")
PAGE = re.compile(r"^[A-Za-z0-9_=-]{1,512}$")
TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
