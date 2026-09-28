CREATE TABLE IF NOT EXISTS runs (
    id             uuid PRIMARY KEY,
    root_run_id    uuid NOT NULL,
    caller         text NOT NULL,
    message_id     text NOT NULL,
    task_id        text NOT NULL,
    agent          text NOT NULL,
    estimated_cost numeric(12, 2) NOT NULL CHECK (estimated_cost >= 0),
    status         text NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running', 'succeeded', 'failed', 'canceled')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (caller, message_id)
);

CREATE INDEX IF NOT EXISTS runs_running_by_caller ON runs (caller) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS runs_by_root ON runs (root_run_id);

-- Retries create new A2A tasks for the same run; every task maps to its run so that
-- canceling any of them reaches the run.
CREATE TABLE IF NOT EXISTS run_tasks (
    task_id text PRIMARY KEY,
    run_id  uuid NOT NULL REFERENCES runs (id)
);

-- Outbox for task notifications: a finished run's tasks are notified until delivery succeeds.
ALTER TABLE run_tasks ADD COLUMN IF NOT EXISTS notified_at timestamptz;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS detail text;

-- A succeeded run's merge request is opened (or found unnecessary) before its tasks hear of
-- it; until then the run stays unsettled and its notifications wait in the outbox.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS proposal_settled_at timestamptz;

-- The reconciler looks for these every pass; both sets stay small while the tables only grow.
CREATE INDEX IF NOT EXISTS runs_unsettled ON runs (id)
    WHERE status = 'succeeded' AND proposal_settled_at IS NULL;
CREATE INDEX IF NOT EXISTS run_tasks_unnotified ON run_tasks (run_id) WHERE notified_at IS NULL;

-- A succeeded run's result waiting for a person (ADR 0015). Only the merge_request kind is
-- recorded so far: its decision is taken in GitLab, and the reconciler follows it there.
CREATE TABLE IF NOT EXISTS proposals (
    id             uuid PRIMARY KEY,
    run_id         uuid NOT NULL UNIQUE REFERENCES runs (id),
    task_id        text NOT NULL,
    agent          text NOT NULL,
    owner          text NOT NULL,
    kind           text NOT NULL
                   CHECK (kind IN ('merge_request', 'wiki_edit', 'desk_reply', 'tracker_issue')),
    state          text NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending', 'accepted', 'applied', 'rejected', 'stale',
                                    'failed')),
    payload        jsonb NOT NULL DEFAULT '{}',
    target         text,
    url            text,
    decided_by     text,
    decided_at     timestamptz,
    detail         text,
    checked_at     timestamptz,
    -- The outbox of state changes: the row is notified to the task service until it equals state.
    notified_state text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS proposals_pending_merge_requests ON proposals (checked_at)
    WHERE kind = 'merge_request' AND state = 'pending';

-- A goal run that found nothing to propose (ADR 0017): its outcome, the record its report is,
-- and the report once read. The branch is deleted after the report is read.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS outcome text;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS record text;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS report text;
