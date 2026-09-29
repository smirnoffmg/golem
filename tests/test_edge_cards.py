import pytest
from google.protobuf.json_format import MessageToDict

from golem.catalog import AgentCatalog, ProcessCatalog
from golem.edge.cards import build_public_card

BASE_URL = "https://golem.example.test"
OIDC_URL = "https://idp.example.test/.well-known/openid-configuration"

CATALOG = AgentCatalog.model_validate(
    {
        "name": "discovery",
        "description": "Turns an epic into written, evidenced product decisions.",
        "version": "0.1.0",
        "skills": [
            {
                "id": "research",
                "name": "Research a hypothesis",
                "description": "Collects evidence for and against a problem hypothesis.",
                "tags": ["discovery"],
            }
        ],
        "kinds": [
            {
                "name": "hypothesis",
                "initial": "proposed",
                "statuses": ["proposed"],
                "sections": ["Evidence"],
            }
        ],
        "roles": [
            {
                "name": "researcher",
                "writes": "hypotheses/",
                "tools": ["tracker.read", "wiki.read"],
            }
        ],
        "rules": [{"role": "researcher", "kind": "hypothesis", "statuses": ["proposed"]}],
    }
)


def card():
    return build_public_card(CATALOG, base_url=BASE_URL, oidc_discovery_url=OIDC_URL)


def test_card_carries_catalog_identity():
    result = card()

    assert (result.name, result.description, result.version) == (
        "discovery",
        CATALOG.description,
        "0.1.0",
    )


def test_card_has_single_jsonrpc_interface_selected_by_tenant():
    [interface] = card().supported_interfaces

    assert interface.url == f"{BASE_URL}/a2a"
    assert interface.protocol_binding == "JSONRPC"
    assert interface.tenant == "discovery"
    assert interface.protocol_version == "1.0"


def test_card_capabilities():
    capabilities = card().capabilities

    assert capabilities.streaming is False
    assert capabilities.push_notifications is True
    # The edge does not forward GetExtendedAgentCard: a client that trusted this flag would
    # get METHOD_NOT_FOUND.
    assert capabilities.extended_agent_card is False


def test_card_requires_oidc():
    result = card()

    scheme = result.security_schemes["oidc"]
    assert scheme.WhichOneof("scheme") == "open_id_connect_security_scheme"
    assert scheme.open_id_connect_security_scheme.open_id_connect_url == OIDC_URL
    [requirement] = result.security_requirements
    assert list(requirement.schemes) == ["oidc"]
    assert list(requirement.schemes["oidc"].list) == []


def test_card_lists_skills():
    [skill] = card().skills

    assert skill.id == "research"
    assert skill.name == "Research a hypothesis"
    assert skill.description == CATALOG.skills[0].description
    assert list(skill.tags) == ["discovery"]


def test_card_default_modes_are_plain_text():
    result = card()

    assert list(result.default_input_modes) == ["text/plain"]
    assert list(result.default_output_modes) == ["text/plain"]


def test_card_hides_kinds_roles_rules_write_paths_and_tools():
    serialized = str(MessageToDict(card()))

    for secret in (
        "researcher",
        "hypotheses/",
        "tracker.read",
        "wiki.read",
        "proposed",
        "Evidence",
    ):
        assert secret not in serialized


def test_trailing_slash_in_base_url_is_rejected():
    with pytest.raises(ValueError, match="trailing slash"):
        build_public_card(CATALOG, base_url=f"{BASE_URL}/", oidc_discovery_url=OIDC_URL)


def test_a_process_card_looks_like_an_agents_so_a_caller_cannot_tell_them_apart():
    process = ProcessCatalog.model_validate(
        {
            "name": "corsar-feature",
            "description": "Takes a change request from analysis to a merged implementation.",
            "version": "0.2.0",
            "skills": [{"id": "feature", "name": "Implement a change", "description": "d"}],
            "stages": [{"name": "analysis", "agent": "analyst", "goal": "{input}"}],
        }
    )

    result = build_public_card(process, base_url=BASE_URL, oidc_discovery_url=OIDC_URL)
    [interface] = result.supported_interfaces
    [skill] = result.skills

    assert (result.name, result.version) == ("corsar-feature", "0.2.0")
    assert interface.tenant == "corsar-feature"
    assert skill.id == "feature"
    assert "analyst" not in str(result)
