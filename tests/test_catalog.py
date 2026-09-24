import pytest
from pydantic import ValidationError

from golem.catalog import AgentCatalog, EmptySection, NoLinked, load_catalog

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
    statuses: [proposed, validated, rejected]
    sections: [Problem, Evidence]
  - name: solution
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
    {"name": "hypothesis", "statuses": ["proposed", "validated"], "sections": ["Evidence"]},
    {"name": "solution", "statuses": ["proposed", "accepted"], "sections": ["Review"]},
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
