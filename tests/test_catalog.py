import pytest
from pydantic import ValidationError

from golem.catalog import (
    DELEGATE_GROUP,
    AgentCatalog,
    EmptySection,
    Kind,
    NoLinked,
    Role,
    load_catalog,
)

CATALOG_YAML = """
name: discovery
description: Turns an epic into written, evidenced product decisions.
version: 0.1.0
skills:
  - id: research
    name: Research a hypothesis
    description: Collects evidence for and against a problem hypothesis.
    tags: [discovery]
kinds:
  - name: hypothesis
    initial: proposed
    statuses: [proposed, validated, rejected]
    sections: [Problem, Evidence]
  - name: solution
    initial: proposed
    statuses: [proposed, accepted, rejected]
    sections: [Proposal, Review]
roles:
  - name: researcher
    writes: hypotheses/
    tools: [tracker.read, wiki.read]
rules:
  - role: researcher
    kind: hypothesis
    statuses: [proposed]
    conditions:
      - type: empty_section
        section: Evidence
      - type: no_linked
        kind: solution
        statuses: [proposed, accepted]
"""


def test_load_catalog_parses_rules_and_conditions(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text(CATALOG_YAML)

    catalog = load_catalog(path)

    assert catalog.name == "discovery"
    [rule] = catalog.rules
    assert rule.statuses == frozenset({"proposed"})
    assert rule.conditions == (
        EmptySection(section="Evidence"),
        NoLinked(kind="solution", statuses=frozenset({"proposed", "accepted"})),
    )


KINDS = [
    {
        "name": "hypothesis",
        "initial": "proposed",
        "statuses": ["proposed", "validated"],
        "sections": ["Evidence"],
    },
    {
        "name": "solution",
        "initial": "proposed",
        "statuses": ["proposed", "accepted"],
        "sections": ["Review"],
    },
]
ROLES = [{"name": "researcher", "writes": "hypotheses/"}]


def catalog_with_rule(**rule: object) -> AgentCatalog:
    return AgentCatalog(
        name="discovery",
        description="d",
        version="0.1.0",
        kinds=KINDS,
        roles=ROLES,
        rules=[{"role": "researcher", **rule}],
    )


def test_a_rule_with_declared_kind_status_and_section_loads():
    catalog = catalog_with_rule(
        kind="hypothesis",
        statuses=["proposed"],
        conditions=[
            {"type": "empty_section", "section": "Evidence"},
            {"type": "no_linked", "kind": "solution", "statuses": ["accepted"]},
        ],
    )

    assert catalog.rules[0].kind == "hypothesis"


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ({"kind": "hypothesys", "statuses": ["proposed"]}, "undeclared kind 'hypothesys'"),
        ({"kind": "hypothesis", "statuses": ["propossed"]}, "undeclared status"),
        (
            {
                "kind": "hypothesis",
                "statuses": ["proposed"],
                "conditions": [{"type": "empty_section", "section": "Evidense"}],
            },
            "undeclared section 'Evidense'",
        ),
        (
            {
                "kind": "hypothesis",
                "statuses": ["proposed"],
                "conditions": [{"type": "no_linked", "kind": "soluton", "statuses": ["accepted"]}],
            },
            "undeclared kind 'soluton'",
        ),
        (
            {
                "kind": "hypothesis",
                "statuses": ["proposed"],
                "conditions": [{"type": "no_linked", "kind": "solution", "statuses": ["done"]}],
            },
            "undeclared status",
        ),
    ],
)
def test_a_typo_in_a_rule_fails_loading_instead_of_idling_forever(rule, message):
    with pytest.raises(ValidationError, match=message):
        catalog_with_rule(**rule)


def test_rule_must_name_a_declared_role():
    with pytest.raises(ValidationError, match="undeclared role"):
        AgentCatalog(
            name="discovery",
            description="d",
            version="0.1.0",
            kinds=KINDS,
            roles=[],
            rules=[{"role": "ghost", "kind": "hypothesis", "statuses": ["proposed"]}],
        )


def test_name_must_be_a_slug():
    with pytest.raises(ValidationError):
        AgentCatalog(name="Discovery Agent", description="d", version="0.1.0")


def test_a_role_may_name_the_delegation_group_like_any_tool_group():
    role = Role(name="planner", writes="hypotheses/", tools=(DELEGATE_GROUP, "wiki.read"))

    assert role.tools == ("agents.delegate", "wiki.read")


@pytest.mark.parametrize(
    "tools",
    [("Agents Delegate",), ("agents.",), ("wiki.read", "wiki.read"), ("",)],
    ids=repr,
)
def test_a_role_names_each_tool_group_once_and_by_its_dotted_name(tools):
    with pytest.raises(ValidationError):
        Role(name="planner", writes="hypotheses/", tools=tools)


@pytest.mark.parametrize("writes", ["hypotheses/", "hypotheses", "docs/plans/", "v1.2_notes"])
def test_a_role_writes_under_a_relative_directory(writes):
    assert Role(name="planner", writes=writes).writes == writes


@pytest.mark.parametrize(
    "writes",
    # `.git` would let the role rewrite git's config; `.` and `` pass every path through the
    # outside-writes check; glob characters widen the file tools' allow rule.
    ["", ".", "./", ".git", ".git/", "..", "a/../b", "/abs", "a//b", "a/.hidden", "a/*", "[ab]"],
    ids=repr,
)
def test_a_role_writes_directory_is_no_dot_segment_glob_or_absolute_path(writes):
    with pytest.raises(ValidationError):
        Role(name="planner", writes=writes)


def test_a_kinds_initial_status_is_one_of_its_statuses():
    with pytest.raises(ValidationError, match="initial"):
        Kind(name="hypothesis", initial="done", statuses=frozenset({"proposed", "validated"}))


# Goal agents (ADR 0017) and the proposal kind (ADR 0015)

CONTEXT = {"url": "https://git.example.com/ctx.git"}


def agent(**fields: object) -> AgentCatalog:
    return AgentCatalog.model_validate(
        {"name": "analyst", "description": "d", "version": "0.1.0", **fields}
    )


def goal_agent(**fields: object) -> AgentCatalog:
    base = {
        "context": CONTEXT,
        "kinds": KINDS,
        "roles": ROLES,
        "mode": "goal",
        "goal": {"role": "researcher", "kind": "hypothesis"},
    }
    return agent(**{**base, **fields})


def test_an_agent_works_on_records_unless_it_says_goal():
    catalog = agent()

    assert (catalog.mode, catalog.goal) == ("records", None)


def test_a_goal_agent_names_the_role_and_the_kind_of_its_target():
    catalog = goal_agent()

    assert catalog.mode == "goal"
    assert (catalog.goal.role, catalog.goal.kind) == ("researcher", "hypothesis")


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"goal": {"role": "ghost", "kind": "hypothesis"}}, "undeclared role 'ghost'"),
        ({"goal": {"role": "researcher", "kind": "memo"}}, "undeclared kind 'memo'"),
        ({"goal": None}, "names no goal"),
        ({"context": None}, "context repository"),
    ],
)
def test_a_goal_agent_is_refused_when_its_goal_cannot_run(fields, message):
    with pytest.raises(ValidationError, match=message):
        goal_agent(**fields)


def test_a_records_agent_may_not_carry_a_goal():
    with pytest.raises(ValidationError, match="mode: goal"):
        agent(kinds=KINDS, roles=ROLES, goal={"role": "researcher", "kind": "hypothesis"})


def test_the_proposal_kind_defaults_to_a_merge_request_and_is_one_of_four():
    assert agent().proposal == "merge_request"
    assert agent(proposal="tracker_issue").proposal == "tracker_issue"
    with pytest.raises(ValidationError):
        agent(proposal="email")


# Neighbours (ADR 0019)

DELEGATING_ROLES = [{"name": "researcher", "writes": "hypotheses/", "tools": [DELEGATE_GROUP]}]


def neighbour(name: str, when: str = "The change touches a contract.") -> dict[str, str]:
    return {"agent": name, "when": when}


def test_an_agent_declares_the_neighbours_its_roles_may_delegate_to():
    catalog = agent(roles=DELEGATING_ROLES, delegates=[neighbour("checker"), neighbour("writer")])

    assert [(n.agent, n.when) for n in catalog.delegates] == [
        ("checker", "The change touches a contract."),
        ("writer", "The change touches a contract."),
    ]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"roles": ROLES, "delegates": [neighbour("checker")]}, "no role holds"),
        ({"roles": DELEGATING_ROLES}, "declares no delegates"),
        (
            {"roles": DELEGATING_ROLES, "delegates": [neighbour(f"a{i}") for i in range(8)]},
            "at most 7",
        ),
        (
            {"roles": DELEGATING_ROLES, "delegates": [neighbour("checker")] * 2},
            "twice",
        ),
        ({"roles": DELEGATING_ROLES, "delegates": [neighbour("analyst")]}, "itself"),
        ({"roles": DELEGATING_ROLES, "delegates": [neighbour("checker", "")]}, "when"),
        ({"roles": DELEGATING_ROLES, "delegates": [neighbour("checker", "x" * 301)]}, "when"),
        ({"roles": DELEGATING_ROLES, "delegates": [neighbour("Checker")]}, "agent"),
    ],
)
def test_neighbours_are_few_named_once_and_go_with_the_delegation_group(fields, message):
    with pytest.raises(ValidationError, match=message):
        agent(**fields)


def _with(extra: str) -> AgentCatalog:
    import yaml

    return AgentCatalog.model_validate(yaml.safe_load(CATALOG_YAML + extra))


def test_reviewers_are_named_people():
    catalog = _with("reviewers: [user:alice, user:bob]\n")

    assert catalog.reviewers == ("user:alice", "user:bob")


def test_an_agent_has_no_reviewers_by_default():
    assert _with("").reviewers == ()


@pytest.mark.parametrize(
    "reviewer", ["service:jira", "agent:discovery", "user:*", "alice", "user:", "user:a b"]
)
def test_reviewers_other_than_a_named_person_are_refused(reviewer):
    # A service or an agent never decides (ADR 0015), and a wildcard would make everyone one.
    with pytest.raises(ValidationError, match="reviewer"):
        _with(f"reviewers: ['{reviewer}']\n")


def test_a_reviewer_listed_twice_is_refused():
    with pytest.raises(ValidationError, match="twice"):
        _with("reviewers: [user:alice, user:alice]\n")
