from a2a.helpers.proto_helpers import new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types.a2a_pb2 import TaskState

from golem.tasks.ports import Orchestrator, Refused, RunStart, Started

MISSING_AGENT = "no agent named: the request carries no tenant"


def caller_of(call_context: ServerCallContext) -> str:
    user = call_context.user
    return user.user_name if user.is_authenticated else "anonymous"


def run_start_of(context: RequestContext) -> RunStart:
    return RunStart(
        task_id=context.task_id or "",
        context_id=context.context_id or "",
        agent=context.tenant,
        goal=context.get_user_input(),
        caller=caller_of(context.call_context),
        message_id=context.message.message_id if context.message is not None else "",
    )


async def reject(updater: TaskUpdater, reason: str) -> None:
    await updater.reject(updater.new_agent_message([new_text_part(reason)]))


class RunExecutor(AgentExecutor):
    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orchestrator = orchestrator

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # a2a-sdk calls execute() again for every message sent into an existing task;
        # one task is one run, so a follow-up must never start a second Job.
        if context.current_task is not None:
            return
        if context.message is not None:
            await event_queue.enqueue_event(new_task_from_user_message(context.message))
        updater = TaskUpdater(event_queue, context.task_id or "", context.context_id or "")
        if not context.tenant:
            await reject(updater, MISSING_AGENT)
            return
        await updater.start_work()
        match await self._orchestrator.start(run_start_of(context)):
            case Refused(reason=reason):
                await reject(updater, reason)
            case Started(run_id=run_id):
                await updater.update_status(
                    TaskState.TASK_STATE_WORKING, metadata={"runId": run_id}
                )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ""
        await self._orchestrator.cancel(task_id)
        await TaskUpdater(event_queue, task_id, context.context_id or "").cancel()
