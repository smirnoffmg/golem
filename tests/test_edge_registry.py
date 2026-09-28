"""The call registry the edge runs with: the file's people and services, the catalogs' agents."""

import pytest

from golem.catalog import AgentCatalog, Catalogs, ProcessCatalog
from golem.edge.policy import Allow, Call, ChainLimits, Deny, Registry, callable_agents, evaluate
from golem.edge.registry import RegistryError, derive_registry

CONTEXT = {"url": "https://git.example.com/corsar/context.git"}
LIMITS = ChainLimits(max_depth=3)


def goal_agent(name: str, *delegates: str) -> AgentCatalog:
    tools = ["agents.delegate"] if delegates else []
    return AgentCatalog.model_validate(
        {
            "name": name,
            "description": name,
            "version": "0.1.0",
            "context": CONTEXT,
            "kinds": [{"name": "change", "initial": "open", "statuses": ["open"]}],
            "roles": [{"name": "worker", "writes": "changes/", "tools": tools}],
            "mode": "goal",
            "goal": {"role": "worker", "kind": "change"},
            "proposal": "merge_request",
            "delegates": [{"agent": d, "when": f"ask {d}"} for d in delegates],
        }
    )


FEATURE = ProcessCatalog.model_validate(
    {
        "name": "corsar-feature",
        "description": "d",
        "version": "0.1.0",
        "stages": [
            {"name": "analysis", "agent": "analyst", "goal": "{input}"},
            {"name": "implementation", "agent": "developer", "goal": "Implement it."},
        ],
    }
)
CATALOGS = Catalogs(
    agents={
        a.name: a
        for a in (
            goal_agent("analyst"),
            goal_agent("developer", "checker", "writer"),
            goal_agent("checker"),
            goal_agent("writer"),
        )
    },
    processes={FEATURE.name: FEATURE},
)
FILE = Registry(allowed_callers={"corsar-feature": frozenset({"user:*"})})


def allowed(registry: Registry, caller: str, callee: str, chain: tuple[str, ...] = ()) -> bool:
    return isinstance(evaluate(Call(caller, callee, chain), registry, LIMITS), Allow)


def test_a_process_may_start_its_stage_agents_and_nothing_else():
    registry = derive_registry(FILE, CATALOGS, LIMITS)

    assert allowed(registry, "agent:corsar-feature", "analyst", ("corsar-feature",))
    assert allowed(registry, "agent:corsar-feature", "developer", ("corsar-feature",))
    assert not allowed(registry, "agent:corsar-feature", "checker", ("corsar-feature",))


def test_an_agent_may_call_its_neighbours_and_nothing_else():
    registry = derive_registry(FILE, CATALOGS, LIMITS)
    chain = ("corsar-feature", "developer")

    assert allowed(registry, "agent:developer", "checker", chain)
    assert allowed(registry, "agent:developer", "writer", chain)
    assert not allowed(registry, "agent:developer", "analyst", chain)
    assert not allowed(registry, "agent:checker", "writer", ("corsar-feature", "checker"))


def test_the_derived_entries_add_to_the_files_callers_of_the_same_agent():
    with_service = Registry(
        allowed_callers={
            "corsar-feature": frozenset({"user:*"}),
            "analyst": frozenset({"service:golem-alertmanager-adapter"}),
        }
    )

    registry = derive_registry(with_service, CATALOGS, LIMITS)

    assert registry.allowed_callers["analyst"] == frozenset(
        {"service:golem-alertmanager-adapter", "agent:corsar-feature"}
    )


def test_people_see_only_processes_in_the_directory():
    registry = derive_registry(FILE, CATALOGS, LIMITS)
    names = sorted([*CATALOGS.agents, *CATALOGS.processes])

    assert callable_agents("user:alice", (), names, registry, LIMITS) == ("corsar-feature",)


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ({"analyst": ["agent:developer"]}, "agent callers come from the catalogs"),
        ({"analyst": ["user:alice"]}, "'analyst' is not a process"),
        ({"analyst": ["user:*"]}, "'analyst' is not a process"),
        ({"ghost": ["service:ci"]}, "'ghost', which no pinned catalog defines"),
    ],
)
def test_the_file_is_refused_when_it_grants_what_catalogs_decide(entries, message):
    file = Registry(allowed_callers={k: frozenset(v) for k, v in entries.items()})

    with pytest.raises(RegistryError, match=message):
        derive_registry(file, CATALOGS, LIMITS)


def test_a_process_needs_a_chain_of_at_least_three():
    with pytest.raises(RegistryError, match="GOLEM_MAX_CHAIN_DEPTH"):
        derive_registry(FILE, CATALOGS, ChainLimits(max_depth=2))


def test_without_a_pinned_process_people_may_still_start_agents():
    # Until a deployment pins its first process there is nothing else for a person to start.
    agents_only = Catalogs(agents={"analyst": goal_agent("analyst")})
    file = Registry(allowed_callers={"analyst": frozenset({"user:*"})})

    registry = derive_registry(file, agents_only, ChainLimits(max_depth=1))

    assert allowed(registry, "user:alice", "analyst")


def test_an_agent_file_entry_is_refused_even_without_a_process():
    agents_only = Catalogs(agents={"analyst": goal_agent("analyst")})
    file = Registry(allowed_callers={"analyst": frozenset({"agent:other"})})

    with pytest.raises(RegistryError, match="agent callers come from the catalogs"):
        derive_registry(file, agents_only, LIMITS)


def test_a_person_may_not_start_a_worker_directly():
    registry = derive_registry(FILE, CATALOGS, LIMITS)

    decision = evaluate(Call("user:alice", "analyst", ()), registry, LIMITS)

    assert isinstance(decision, Deny)
    assert "may not call agent 'analyst'" in decision.detail
