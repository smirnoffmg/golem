from dataclasses import asdict, dataclass

import httpx

from golem.orchestrator.reconcile import TaskOutcome

OUTCOME_PATH = "/internal/run-outcome"


@dataclass(frozen=True)
class TaskServiceNotifier:
    client: httpx.AsyncClient

    async def notify(self, outcome: TaskOutcome) -> bool:
        try:
            response = await self.client.post(OUTCOME_PATH, json=asdict(outcome))
        except httpx.HTTPError:
            return False
        # 404: the task is gone (e.g. its store was reset); retrying would never succeed.
        return response.status_code in (200, 404)
