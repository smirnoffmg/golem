from dataclasses import dataclass
from decimal import Decimal

from psycopg import AsyncConnection

from golem.orchestrator.admission import Limits, Rejected
from golem.orchestrator.runs import RunReused, StartRequest, cancel_run_of_task, start_run
from golem.tasks.ports import Refused, RunStart, Started


@dataclass(frozen=True)
class PostgresOrchestrator:
    """The task service's orchestrator port, backed by the runs database.

    Every run is estimated at the same flat cost until estimates come from the agent catalog.
    """

    dsn: str
    limits: Limits
    estimated_cost: Decimal

    async def start(self, run: RunStart) -> Started | Refused:
        request = StartRequest(
            caller=run.caller,
            message_id=run.message_id,
            task_id=run.task_id,
            agent=run.agent,
            estimated_cost=self.estimated_cost,
        )
        async with await AsyncConnection.connect(self.dsn, autocommit=True) as conn:
            outcome = await start_run(conn, request, self.limits)
        if isinstance(outcome, Rejected):
            return Refused(reason=outcome.detail)
        if isinstance(outcome, RunReused) and outcome.status != "running":
            # A retry of a finished run must not leave a task WORKING that nothing will finish.
            return Refused(
                reason=f"Run {outcome.run_id} for this message is already {outcome.status}."
            )
        return Started(run_id=outcome.run_id)

    async def cancel(self, task_id: str) -> None:
        async with await AsyncConnection.connect(self.dsn, autocommit=True) as conn:
            await cancel_run_of_task(conn, task_id)
