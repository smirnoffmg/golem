from dataclasses import asdict, dataclass

import httpx

from golem.orchestrator.reconcile import TaskOutcome

OUTCOME_PATH = "/internal/run-outcome"
PROPOSAL_STATE_PATH = "/internal/proposal-state"


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
                PROPOSAL_STATE_PATH, json={"proposal_id": proposal_id}
            )
        except httpx.HTTPError:
            return False
        return response.status_code in (200, 404)
