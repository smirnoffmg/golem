import pytest

from golem.mcp.atlassian import Deployment, JiraDeployment
from golem.mcp.settings import McpSettings, mcp_settings
from golem.ratelimit import Rate
from golem.settings import SettingsError

TRACKER_ENV = {
    "GOLEM_MCP_GROUP": "tracker.read",
    "GOLEM_MCP_UPSTREAM_URL": "https://jira.example.test",
    "GOLEM_MCP_UPSTREAM_TOKEN": "service-pat",
    "GOLEM_MCP_JIRA_DEPLOYMENT": "data-center",
    "GOLEM_TASK_SERVICE_URL": "http://tasks.golem.svc:8000",
    "GOLEM_AUDIT_DSN": "host=pg dbname=golem_audit user=golem_mcp password=x",
}


def test_settings_for_the_tracker_group() -> None:
    settings = mcp_settings(TRACKER_ENV)

    assert settings == McpSettings(
        group="tracker.read",
        upstream_url="https://jira.example.test",
        upstream_user=None,
        upstream_token="service-pat",
        jira_deployment=JiraDeployment.DATA_CENTER,
        task_service_url="http://tasks.golem.svc:8000",
        audit_dsn=TRACKER_ENV["GOLEM_AUDIT_DSN"],
        run_status_ttl_seconds=10.0,
        keys_refresh_seconds=60.0,
        port=8000,
        auth_failure_rate=Rate(per_minute=30, burst=10),
        trusted_proxies=(),
    )
    assert "service-pat" not in repr(settings)
    assert "password" not in repr(settings)


def test_settings_for_the_wiki_group_need_no_jira_deployment() -> None:
    env = {
        **{k: v for k, v in TRACKER_ENV.items() if k != "GOLEM_MCP_JIRA_DEPLOYMENT"},
        "GOLEM_MCP_GROUP": "wiki.read",
        "GOLEM_MCP_UPSTREAM_URL": "https://example.atlassian.net/wiki",
        "GOLEM_MCP_UPSTREAM_USER": "bot@example.test",
        "GOLEM_MCP_RUN_STATUS_TTL_SECONDS": "2.5",
        "GOLEM_MCP_KEYS_REFRESH_SECONDS": "30",
        "GOLEM_PORT": "8090",
    }

    settings = mcp_settings(env)

    assert settings.group == "wiki.read"
    assert settings.jira_deployment is None
    assert settings.upstream_user == "bot@example.test"
    assert (settings.run_status_ttl_seconds, settings.keys_refresh_seconds) == (2.5, 30.0)
    assert settings.port == 8090


def test_every_missing_variable_is_named_at_once() -> None:
    with pytest.raises(SettingsError) as caught:
        mcp_settings({"GOLEM_MCP_GROUP": "tracker.read"})

    for name in (
        "GOLEM_MCP_UPSTREAM_URL",
        "GOLEM_MCP_UPSTREAM_TOKEN",
        "GOLEM_MCP_JIRA_DEPLOYMENT",
        "GOLEM_TASK_SERVICE_URL",
        "GOLEM_AUDIT_DSN",
    ):
        assert name in str(caught.value)


def test_a_missing_group_is_named_with_the_rest() -> None:
    with pytest.raises(SettingsError, match=r"GOLEM_MCP_GROUP.*GOLEM_AUDIT_DSN"):
        mcp_settings({})


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"GOLEM_MCP_GROUP": "tracker.delete"}, "GOLEM_MCP_GROUP must be one of"),
        ({"GOLEM_MCP_JIRA_DEPLOYMENT": "server"}, "GOLEM_MCP_JIRA_DEPLOYMENT must be one of"),
        ({"GOLEM_MCP_UPSTREAM_URL": "https://jira.example.test/"}, "must not end with a slash"),
        ({"GOLEM_MCP_UPSTREAM_URL": "jira.example.test"}, "must be an http"),
        ({"GOLEM_TASK_SERVICE_URL": "http://tasks/"}, "must not end with a slash"),
        ({"GOLEM_MCP_RUN_STATUS_TTL_SECONDS": "-1"}, "must not be negative"),
        ({"GOLEM_MCP_KEYS_REFRESH_SECONDS": "soon"}, "must be a number"),
        ({"GOLEM_PORT": "70000"}, "GOLEM_PORT"),
    ],
)
def test_malformed_values_are_refused(override: dict[str, str], message: str) -> None:
    with pytest.raises(SettingsError, match=message):
        mcp_settings({**TRACKER_ENV, **override})


WIKI_WRITE_ENV = {
    **{k: v for k, v in TRACKER_ENV.items() if k != "GOLEM_MCP_JIRA_DEPLOYMENT"},
    "GOLEM_MCP_GROUP": "wiki.write",
    "GOLEM_MCP_UPSTREAM_URL": "https://confluence.example.test",
    "GOLEM_MCP_CONFLUENCE_DEPLOYMENT": "data-center",
    "GOLEM_MCP_RESOURCE": "http://mcp-wiki-write.golem-system.svc:8000/mcp",
    "GOLEM_MCP_WIKI_SPACES": "OPS, DOCS",
}


def test_a_write_server_names_its_audience_and_where_it_writes() -> None:
    settings = mcp_settings(WIKI_WRITE_ENV)

    assert settings.group == "wiki.write"
    assert settings.confluence_deployment is Deployment.DATA_CENTER
    assert settings.resource == "http://mcp-wiki-write.golem-system.svc:8000/mcp"
    assert settings.allowed == frozenset({"OPS", "DOCS"})
    assert settings.proposal_status_ttl_seconds == 10.0


@pytest.mark.parametrize(
    ("group", "names"),
    [
        (
            "wiki.write",
            ("GOLEM_MCP_CONFLUENCE_DEPLOYMENT", "GOLEM_MCP_RESOURCE", "GOLEM_MCP_WIKI_SPACES"),
        ),
        ("desk.write", ("GOLEM_MCP_RESOURCE", "GOLEM_MCP_DESK_PROJECTS")),
        (
            "tracker.write",
            ("GOLEM_MCP_JIRA_DEPLOYMENT", "GOLEM_MCP_RESOURCE", "GOLEM_MCP_TRACKER_PROJECTS"),
        ),
    ],
)
def test_a_write_server_without_its_audience_or_allowlist_does_not_start(
    group: str, names: tuple[str, ...]
) -> None:
    env = {k: v for k, v in TRACKER_ENV.items() if k != "GOLEM_MCP_JIRA_DEPLOYMENT"}

    with pytest.raises(SettingsError) as caught:
        mcp_settings({**env, "GOLEM_MCP_GROUP": group})

    for name in names:
        assert name in str(caught.value)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"GOLEM_MCP_WIKI_SPACES": " , "}, "GOLEM_MCP_WIKI_SPACES must name at least one"),
        ({"GOLEM_MCP_WIKI_SPACES": "OPS,ops team"}, "GOLEM_MCP_WIKI_SPACES: 'ops team'"),
        ({"GOLEM_MCP_RESOURCE": "https://Wiki.example/mcp"}, "canonical URI"),
        ({"GOLEM_MCP_RESOURCE": "https://wiki.example/mcp/"}, "canonical URI"),
        ({"GOLEM_MCP_RESOURCE": "https://wiki.example/mcp#x"}, "canonical URI"),
        ({"GOLEM_MCP_CONFLUENCE_DEPLOYMENT": "server"}, "must be one of"),
    ],
)
def test_malformed_write_settings_are_refused(override: dict[str, str], message: str) -> None:
    with pytest.raises(SettingsError, match=message):
        mcp_settings({**WIKI_WRITE_ENV, **override})


def test_a_read_server_needs_no_audience_or_allowlist() -> None:
    settings = mcp_settings(TRACKER_ENV)

    assert (settings.resource, settings.allowed) == (None, frozenset())
