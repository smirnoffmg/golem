"""The edge as the golden set sees it: delegation calls recorded, never sent (ADR 0019).

A case says which neighbours the role must ask; the runtime's own delegation tool runs with this
recorder as its transport, so what is checked is what the tool would have sent in production.
"""

import json
from dataclasses import dataclass, field

import httpx


@dataclass
class DelegationRecorder:
    calls: list[str] = field(default_factory=list)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._answer)

    def take(self) -> tuple[str, ...]:
        taken, self.calls = tuple(self.calls), []
        return taken

    def _answer(self, request: httpx.Request) -> httpx.Response:
        agent = json.loads(request.content)["params"]["tenant"]
        self.calls.append(agent)
        task = {"id": f"evaluation-{len(self.calls)}", "status": {"state": "TASK_STATE_SUBMITTED"}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"task": task}})
