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


def test_rule_must_name_a_declared_role():
    with pytest.raises(ValidationError, match="undeclared role"):
        AgentCatalog(
            name="discovery",
            description="d",
            version="0.1.0",
            roles=[],
            rules=[{"role": "ghost", "kind": "hypothesis", "statuses": ["proposed"]}],
        )


def test_name_must_be_a_slug():
    with pytest.raises(ValidationError):
        AgentCatalog(name="Discovery Agent", description="d", version="0.1.0")
