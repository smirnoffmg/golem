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
