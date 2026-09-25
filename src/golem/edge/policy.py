from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

AGENT_PREFIX = "agent:"


@dataclass(frozen=True)
class Call:
    caller: str
    callee: str
    chain: tuple[str, ...]


@dataclass(frozen=True)
class Registry:
    allowed_callers: Mapping[str, frozenset[str]]


@dataclass(frozen=True)
class ChainLimits:
    max_depth: int


class DenyReason(Enum):
    UNKNOWN_AGENT = "unknown_agent"
    CYCLE = "cycle"
    DEPTH_EXCEEDED = "depth_exceeded"
    NOT_ALLOWED = "not_allowed"
    MALFORMED_CHAIN = "malformed_chain"


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Deny:
    reason: DenyReason
    detail: str


def evaluate(call: Call, registry: Registry, limits: ChainLimits) -> Allow | Deny:
    if not _chain_matches_caller(call):
        return Deny(
            DenyReason.MALFORMED_CHAIN,
            f"chain {list(call.chain)} does not end with caller {call.caller!r}",
        )
    allowed = registry.allowed_callers.get(call.callee)
    if allowed is None:
        return Deny(DenyReason.UNKNOWN_AGENT, f"agent {call.callee!r} is not registered")
    if call.callee in call.chain:
        return Deny(DenyReason.CYCLE, f"agent {call.callee!r} is already in chain")
    depth = len(call.chain) + 1
    if depth > limits.max_depth:
        return Deny(
            DenyReason.DEPTH_EXCEEDED,
            f"chain depth {depth} exceeds limit {limits.max_depth}",
        )
    if not _is_allowed(call.caller, allowed):
        return Deny(
            DenyReason.NOT_ALLOWED,
            f"{call.caller!r} may not call agent {call.callee!r}",
        )
    return Allow()


def callable_agents(
    caller: str,
    chain: tuple[str, ...],
    agents: Iterable[str],
    registry: Registry,
    limits: ChainLimits,
) -> tuple[str, ...]:
    """The ``agents`` a call by ``caller`` with ``chain`` would be allowed to reach: the
    directory's view of the same rules, so it never shows what a call would be refused."""
    return tuple(
        agent
        for agent in agents
        if isinstance(
            evaluate(Call(caller=caller, callee=agent, chain=chain), registry, limits), Allow
        )
    )


def _chain_matches_caller(call: Call) -> bool:
    if call.caller.startswith(AGENT_PREFIX):
        agent = call.caller.removeprefix(AGENT_PREFIX)
        return bool(call.chain) and call.chain[-1] == agent
    return not call.chain


def _is_allowed(caller: str, allowed: frozenset[str]) -> bool:
    kind, sep, _ = caller.partition(":")
    return caller in allowed or (bool(sep) and f"{kind}:*" in allowed)
