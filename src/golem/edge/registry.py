"""The call registry the edge runs with (ADR 0019).

The file (``GOLEM_CALL_REGISTRY_FILE``) grants people and services. Who an agent may call is not
written there: it follows from the pinned catalogs, an agent's neighbours and a process's stages,
so it is reviewed where it is declared and cannot drift from what the tool offers.
"""

from collections import defaultdict
from collections.abc import Iterable

from golem.catalog import Catalogs
from golem.edge.policy import AGENT_PREFIX, ChainLimits, Registry

USER_PREFIX = "user:"
# person -> process -> stage agent -> neighbour
PROCESS_DEPTH = 3


class RegistryError(ValueError):
    pass


def derive_registry(file: Registry, catalogs: Catalogs, limits: ChainLimits) -> Registry:
    check_file(file, catalogs)
    if catalogs.processes and limits.max_depth < PROCESS_DEPTH:
        raise RegistryError(
            f"GOLEM_MAX_CHAIN_DEPTH is {limits.max_depth}; a pinned process needs at least"
            f" {PROCESS_DEPTH} (person, process, stage agent, neighbour)"
        )
    callers: dict[str, set[str]] = defaultdict(set)
    for callee, allowed in file.allowed_callers.items():
        callers[callee] |= allowed
    for caller, callees in derived_calls(catalogs):
        for callee in callees:
            callers[callee].add(f"{AGENT_PREFIX}{caller}")
    return Registry(allowed_callers={name: frozenset(c) for name, c in callers.items()})


def derived_calls(catalogs: Catalogs) -> Iterable[tuple[str, tuple[str, ...]]]:
    for agent in catalogs.agents.values():
        yield agent.name, tuple(neighbour.agent for neighbour in agent.delegates)
    for process in catalogs.processes.values():
        yield process.name, tuple(stage.agent for stage in process.stages)


def check_file(file: Registry, catalogs: Catalogs) -> None:
    for callee, allowed in sorted(file.allowed_callers.items()):
        if callee not in catalogs.agents and callee not in catalogs.processes:
            raise RegistryError(
                f"the call registry names callee {callee!r}, which no pinned catalog defines"
            )
        for caller in sorted(allowed):
            if caller.startswith(AGENT_PREFIX):
                raise RegistryError(
                    f"the call registry grants {caller!r} on {callee!r}; agent callers come from"
                    " the catalogs (delegates and process stages), not from the file"
                )
            # A deployment without a process has nothing else for a person to start; once it
            # pins one, workers are the platform's to call and people start processes.
            if (
                caller.startswith(USER_PREFIX)
                and catalogs.processes
                and callee not in catalogs.processes
            ):
                raise RegistryError(
                    f"the call registry grants {caller!r} on {callee!r}, but {callee!r} is not a"
                    " process; people start processes"
                )
