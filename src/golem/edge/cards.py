from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    OpenIdConnectSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)

from golem.catalog import AgentCatalog, Skill

OIDC_SCHEME = "oidc"
PLAIN_TEXT = "text/plain"


def build_public_card(
    catalog: AgentCatalog, *, base_url: str, oidc_discovery_url: str
) -> AgentCard:
    """Only skills are published: roles, rules, write paths and tools stay internal."""
    if base_url.endswith("/"):
        raise ValueError(f"base_url must not have a trailing slash: {base_url!r}")
    return AgentCard(
        name=catalog.name,
        description=catalog.description,
        version=catalog.version,
        supported_interfaces=[
            AgentInterface(
                url=f"{base_url}/a2a",
                protocol_binding="JSONRPC",
                tenant=catalog.name,
                protocol_version="1.0",
            )
        ],
        capabilities=AgentCapabilities(
            streaming=False, push_notifications=True, extended_agent_card=True
        ),
        security_schemes={
            OIDC_SCHEME: SecurityScheme(
                open_id_connect_security_scheme=OpenIdConnectSecurityScheme(
                    open_id_connect_url=oidc_discovery_url
                )
            )
        },
        security_requirements=[SecurityRequirement(schemes={OIDC_SCHEME: StringList()})],
        default_input_modes=[PLAIN_TEXT],
        default_output_modes=[PLAIN_TEXT],
        skills=[_public_skill(skill) for skill in catalog.skills],
    )


def _public_skill(skill: Skill) -> AgentSkill:
    return AgentSkill(
        id=skill.id, name=skill.name, description=skill.description, tags=list(skill.tags)
    )
