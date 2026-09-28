"""Routing in the golden set: the delegation tool's calls recorded instead of sent (ADR 0019)."""

from dataclasses import dataclass
from pathlib import Path

from test_evaluation_run import H2, catalog, make_case

from golem.catalog import Neighbour
from golem.evaluation.delegations import DelegationRecorder
from golem.evaluation.run import run_case
from golem.runtime.delegation import Delegation, delegation_tool
from golem.runtime.ports import Brief, RoleResult

NEIGHBOURS = (
    Neighbour(agent="checker", when="A contract changes."),
    Neighbour(agent="writer", when="A page describes the behaviour."),
)
ROUTING_CASE = """\
goal: Work the discovery backlog
expect:
  outcome: proposed
  delegates: [checker]
"""


async def delegate(recorder: DelegationRecorder, agent: str) -> str:
    tool = delegation_tool(
        Delegation(
            url="http://edge.test/a2a",
            call_token="evaluation",
            run_id="eval-1",
            call_timeout=5,
            max_result_chars=10_000,
            transport=recorder.transport,
        ),
        NEIGHBOURS,
    )
    return await tool.ainvoke({"agent": agent, "goal": "check the contract"})


async def test_the_recorder_answers_like_the_edge_and_keeps_the_agents_asked():
    recorder = DelegationRecorder()

    first = await delegate(recorder, "checker")
    await delegate(recorder, "writer")

    assert "Delegated to checker" in first
    assert "TASK_STATE_SUBMITTED" in first
    assert recorder.take() == ("checker", "writer")
    assert recorder.take() == ()


async def test_a_name_outside_the_neighbours_is_never_recorded():
    recorder = DelegationRecorder()

    await delegate(recorder, "deployer")

    assert recorder.take() == ()


@dataclass
class Delegating:
    """A role that asks its neighbours, then does its own work."""

    recorder: DelegationRecorder
    ask: tuple[str, ...]

    async def run(self, brief: Brief) -> RoleResult:
        for agent in self.ask:
            await delegate(self.recorder, agent)
        brief.target_path.write_text(brief.target_text + "- Interviews support it.\n")
        return RoleResult(summary="done")


async def test_a_case_passes_when_the_role_asks_the_neighbours_it_names(tmp_path: Path):
    case = make_case(tmp_path, "routing", ROUTING_CASE, {"hypotheses/H-2.md": H2})
    recorder = DelegationRecorder()

    result = await run_case(
        case,
        catalog(tmp_path),
        Delegating(recorder, ("checker",)),
        tmp_path / "w",
        delegations=recorder,
    )

    assert result.failures == ()


async def test_a_case_fails_when_the_role_asks_someone_else(tmp_path: Path):
    case = make_case(tmp_path, "routing", ROUTING_CASE, {"hypotheses/H-2.md": H2})
    recorder = DelegationRecorder()
    # A call left over from an earlier case must not count for this one.
    await delegate(recorder, "checker")

    result = await run_case(
        case,
        catalog(tmp_path),
        Delegating(recorder, ("writer",)),
        tmp_path / "w",
        delegations=recorder,
    )

    assert result.failures == ("delegates: expected checker, got writer",)
