from dataclasses import asdict
from typing import Any

from a2a.helpers.proto_helpers import new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types.a2a_pb2 import Message, TaskState

from golem.catalog import GOAL_TARGET
from golem.tasks.ports import Orchestrator, Refused, RunOutcome, RunStart, Started

MISSING_AGENT = "no agent named: the request carries no tenant"
# Set only by the task service's internal outcome route, never from a request body, so a
# caller cannot finish a task by sending a message that claims the run is done.
RUN_OUTCOME = "golem.run_outcome"
AGENT_METADATA = "golemAgent"
PROPOSAL_METADATA = "golemProposal"
OUTCOME_METADATA = "golemOutcome"
TARGET_METADATA = "golemTarget"
REPORT_ARTIFACT = "report"
CHAIN_HEADER = "x-golem-chain"
ROOT_RUN_HEADER = "x-golem-root-run"


ANONYMOUS = "anonymous"


def caller_of(call_context: ServerCallContext) -> str:
    user = call_context.user
    return user.user_name if user.is_authenticated else ANONYMOUS


def run_start_of(context: RequestContext) -> RunStart:
    return RunStart(
        task_id=context.task_id or "",
        context_id=context.context_id or "",
        agent=context.tenant,
        goal=context.get_user_input(),
        caller=caller_of(context.call_context),
        message_id=context.message.message_id if context.message is not None else "",
        traceparent=header_of(context.call_context, "traceparent"),
        tracestate=header_of(context.call_context, "tracestate"),
        root_run_id=header_of(context.call_context, ROOT_RUN_HEADER),
        chain=chain_of(header_of(context.call_context, CHAIN_HEADER)),
        target=target_of(context.message),
    )


def target_of(message: Message | None) -> str:
    # The caller's claim, used only as a record name in the caller's own run: it must be one.
    if message is None or TARGET_METADATA not in message.metadata:
        return ""
    target = message.metadata[TARGET_METADATA]
    return target if isinstance(target, str) and GOAL_TARGET.fullmatch(target) else ""


def chain_of(header: str) -> tuple[str, ...]:
    # Set by the edge from a verified call token; the edge strips any a client sends.
    return tuple(agent for agent in (a.strip() for a in header.split(",")) if agent)


def header_of(call_context: ServerCallContext, name: str) -> str:
    # The edge forwards only a well-formed traceparent and a bounded tracestate.
    return call_context.state.get("headers", {}).get(name, "")


async def reject(updater: TaskUpdater, reason: str) -> None:
    await updater.reject(updater.new_agent_message([new_text_part(reason)]))


async def finish(updater: TaskUpdater, outcome: RunOutcome) -> None:
    message = updater.new_agent_message([new_text_part(outcome.detail)])
    if outcome.succeeded:
        # Merged into the task's metadata by the SDK's TaskManager, as runId is.
        metadata: dict[str, Any] | None = (
            {PROPOSAL_METADATA: asdict(outcome.proposal)} if outcome.proposal else None
        )
        if outcome.report is not None:
            # One id per run, so a delivery repeated before the task ended adds no second one.
            await updater.add_artifact(
                [new_text_part(outcome.report)],
                artifact_id=f"{REPORT_ARTIFACT}-{outcome.run_id}",
                name=REPORT_ARTIFACT,
            )
            metadata = {OUTCOME_METADATA: "reported"}
        await updater.update_status(
            TaskState.TASK_STATE_COMPLETED, message=message, metadata=metadata
        )
    else:
        await updater.failed(message)


class RunExecutor(AgentExecutor):
    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orchestrator = orchestrator

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # a2a-sdk calls execute() again for every message sent into an existing task;
        # one task is one run, so a follow-up must never start a second Job.
        if context.current_task is not None:
            outcome = context.call_context.state.get(RUN_OUTCOME)
            if isinstance(outcome, RunOutcome):
                await finish(
                    TaskUpdater(event_queue, context.task_id or "", context.context_id or ""),
                    outcome,
                )
            return
        if context.message is not None:
            await event_queue.enqueue_event(new_task_from_user_message(context.message))
        updater = TaskUpdater(event_queue, context.task_id or "", context.context_id or "")
        if not context.tenant:
            await reject(updater, MISSING_AGENT)
            return
        # The tenant the edge checked against the call registry, not the caller's own claim:
        # ListTasks filters by it (ADR 0018).
        await updater.update_status(
            TaskState.TASK_STATE_WORKING, metadata={AGENT_METADATA: context.tenant}
        )
        run = run_start_of(context)
        match await self._orchestrator.start(run):
            case Refused(reason=reason):
                await reject(updater, reason)
            case Started(run_id=run_id):
                # The chain comes back with the task: whoever reads it sees which agents
                # took part (ADR 0014).
                chain = {"chain": list(run.chain)} if run.chain else {}
                await updater.update_status(
                    TaskState.TASK_STATE_WORKING, metadata={"runId": run_id, **chain}
                )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ""
        await self._orchestrator.cancel(task_id)
        await TaskUpdater(event_queue, task_id, context.context_id or "").cancel()
