from dataclasses import asdict, dataclass

import httpx

from golem.orchestrator.reconcile import TaskOutcome

OUTCOME_PATH = "/internal/run-outcome"
PROPOSAL_STATE_PATH = "/internal/proposal-state"
PROCESS_STATE_PATH = "/internal/process-state"
# The task service may apply the proposal before it answers, and waits up to 15 s for a write
# server: this call waits longer, or the reconciler would always give up first.
PROPOSAL_STATE_TIMEOUT_SECONDS = 25.0


@dataclass(frozen=True)
class TaskServiceNotifier:
    client: httpx.AsyncClient

    async def notify(self, outcome: TaskOutcome) -> bool:
        try:
            response = await self.client.post(OUTCOME_PATH, json=asdict(outcome))
        except httpx.HTTPError:
            return False
        # 404: the task is gone (e.g. its store was reset); retrying would never succeed. Anything
        # else, 409 included (the task service does not see a final outcome yet), is retried.
        return response.status_code in (200, 404)

    async def notify_proposal(self, proposal_id: str) -> bool:
        try:
            response = await self.client.post(
                PROPOSAL_STATE_PATH,
                json={"proposal_id": proposal_id},
                timeout=PROPOSAL_STATE_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            return False
        return response.status_code in (200, 404)

    async def notify_process(self, process_run_id: str) -> bool:
        try:
            response = await self.client.post(
                PROCESS_STATE_PATH, json={"process_run_id": process_run_id}
            )
        except httpx.HTTPError:
            return False
        return response.status_code in (200, 404)
