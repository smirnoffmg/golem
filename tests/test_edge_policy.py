import pytest

from golem.edge.policy import (
    Allow,
    Call,
    ChainLimits,
    Deny,
    DenyReason,
    Registry,
    callable_agents,
    evaluate,
)

LIMITS = ChainLimits(max_depth=3)


def registry(**allowed: set[str]) -> Registry:
    return Registry(allowed_callers={name: frozenset(callers) for name, callers in allowed.items()})


def reason(result: Allow | Deny) -> DenyReason:
    assert isinstance(result, Deny)
    return result.reason


def test_registered_user_may_call_agent():
    call = Call(caller="user:alice", callee="discovery", chain=())

    assert evaluate(call, registry(discovery={"user:alice"}), LIMITS) == Allow()


def test_registered_agent_may_call_agent():
    call = Call(caller="agent:discovery", callee="delivery", chain=("discovery",))

    assert evaluate(call, registry(delivery={"agent:discovery"}), LIMITS) == Allow()


def test_unregistered_caller_is_denied():
    call = Call(caller="user:mallory", callee="discovery", chain=())

    result = evaluate(call, registry(discovery={"user:alice"}), LIMITS)

    assert reason(result) is DenyReason.NOT_ALLOWED
    assert "user:mallory" in result.detail


@pytest.mark.parametrize(
    ("wildcard", "caller", "chain"),
    [
        ("user:*", "user:bob", ()),
        ("service:*", "service:ci", ()),
        ("agent:*", "agent:other", ("other",)),
    ],
)
def test_wildcard_admits_any_principal_of_its_type(wildcard, caller, chain):
    call = Call(caller=caller, callee="discovery", chain=chain)

    assert evaluate(call, registry(discovery={wildcard}), LIMITS) == Allow()


@pytest.mark.parametrize(
    ("wildcard", "caller", "chain"),
    [
        ("user:*", "service:ci", ()),
        ("service:*", "user:bob", ()),
        ("agent:*", "user:bob", ()),
        ("user:*", "agent:other", ("other",)),
    ],
)
def test_wildcard_does_not_cross_principal_types(wildcard, caller, chain):
    call = Call(caller=caller, callee="discovery", chain=chain)

    assert reason(evaluate(call, registry(discovery={wildcard}), LIMITS)) is DenyReason.NOT_ALLOWED


def test_bare_star_is_not_a_wildcard():
    call = Call(caller="user:bob", callee="discovery", chain=())

    assert reason(evaluate(call, registry(discovery={"*"}), LIMITS)) is DenyReason.NOT_ALLOWED


def test_unknown_callee_is_denied():
    call = Call(caller="user:alice", callee="ghost", chain=())

    result = evaluate(call, registry(discovery={"user:alice"}), LIMITS)

    assert reason(result) is DenyReason.UNKNOWN_AGENT
    assert "ghost" in result.detail


def test_callee_already_in_chain_is_a_cycle():
    call = Call(caller="agent:b", callee="a", chain=("a", "b"))

    assert reason(evaluate(call, registry(a={"agent:*"}), LIMITS)) is DenyReason.CYCLE


def test_self_call_is_a_cycle():
    call = Call(caller="agent:a", callee="a", chain=("a",))

    assert reason(evaluate(call, registry(a={"agent:a"}), LIMITS)) is DenyReason.CYCLE


def test_chain_at_max_depth_is_allowed():
    call = Call(caller="agent:b", callee="c", chain=("a", "b"))

    assert evaluate(call, registry(c={"agent:b"}), LIMITS) == Allow()


def test_chain_beyond_max_depth_is_denied():
    call = Call(caller="agent:c", callee="d", chain=("a", "b", "c"))

    assert reason(evaluate(call, registry(d={"agent:c"}), LIMITS)) is DenyReason.DEPTH_EXCEEDED


def test_agent_caller_must_be_last_in_chain():
    call = Call(caller="agent:b", callee="c", chain=("a",))

    assert reason(evaluate(call, registry(c={"agent:b"}), LIMITS)) is DenyReason.MALFORMED_CHAIN


def test_agent_caller_with_empty_chain_is_malformed():
    call = Call(caller="agent:b", callee="c", chain=())

    assert reason(evaluate(call, registry(c={"agent:b"}), LIMITS)) is DenyReason.MALFORMED_CHAIN


@pytest.mark.parametrize("caller", ["user:alice", "service:ci"])
def test_non_agent_caller_with_chain_is_malformed(caller):
    call = Call(caller=caller, callee="c", chain=("a",))

    assert reason(evaluate(call, registry(c={caller}), LIMITS)) is DenyReason.MALFORMED_CHAIN


def test_malformed_chain_wins_over_unknown_agent():
    call = Call(caller="user:alice", callee="ghost", chain=("a",))

    assert reason(evaluate(call, registry(), LIMITS)) is DenyReason.MALFORMED_CHAIN


def test_unknown_agent_wins_over_cycle():
    call = Call(caller="agent:ghost", callee="ghost", chain=("ghost",))

    assert reason(evaluate(call, registry(), LIMITS)) is DenyReason.UNKNOWN_AGENT


def test_cycle_wins_over_depth():
    call = Call(caller="agent:c", callee="a", chain=("a", "b", "c"))

    assert reason(evaluate(call, registry(a={"agent:c"}), LIMITS)) is DenyReason.CYCLE


def test_depth_wins_over_registry():
    call = Call(caller="agent:c", callee="d", chain=("a", "b", "c"))

    assert reason(evaluate(call, registry(d=set()), LIMITS)) is DenyReason.DEPTH_EXCEEDED


def test_callee_registered_with_no_callers_denies_everyone():
    call = Call(caller="user:alice", callee="discovery", chain=())

    assert reason(evaluate(call, registry(discovery=set()), LIMITS)) is DenyReason.NOT_ALLOWED


# --- The directory: what a caller may call, by the same rules ----------------------------------

DIRECTORY = registry(
    discovery={"user:alice", "agent:reviewer"},
    reviewer={"user:*", "agent:discovery"},
    evaluator={"service:gitlab-ci"},
)
PUBLISHED = ("discovery", "evaluator", "ghost", "reviewer")


def test_a_user_sees_the_agents_the_registry_lets_them_call():
    assert callable_agents("user:alice", (), PUBLISHED, DIRECTORY, LIMITS) == (
        "discovery",
        "reviewer",
    )
    assert callable_agents("user:bob", (), PUBLISHED, DIRECTORY, LIMITS) == ("reviewer",)


def test_a_published_agent_missing_from_the_registry_is_callable_by_nobody():
    assert "ghost" not in callable_agents("user:alice", (), PUBLISHED, DIRECTORY, LIMITS)


def test_an_agent_never_sees_itself_or_an_agent_already_in_its_chain():
    assert callable_agents("agent:discovery", ("discovery",), PUBLISHED, DIRECTORY, LIMITS) == (
        "reviewer",
    )
    assert (
        callable_agents("agent:discovery", ("reviewer", "discovery"), PUBLISHED, DIRECTORY, LIMITS)
        == ()
    )


def test_an_agent_at_the_depth_limit_sees_nothing():
    chain = ("a", "b", "reviewer")

    assert callable_agents("agent:reviewer", chain, PUBLISHED, DIRECTORY, LIMITS) == ()


def test_a_malformed_chain_sees_nothing():
    assert callable_agents("user:alice", ("x",), PUBLISHED, DIRECTORY, LIMITS) == ()
