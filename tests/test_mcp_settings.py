import pytest

from golem.mcp.atlassian import JiraDeployment
from golem.mcp.settings import McpSettings, mcp_settings
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
        ({"GOLEM_MCP_GROUP": "tracker.write"}, "GOLEM_MCP_GROUP must be one of"),
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
