import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from functools import partial
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from a2a.server.tasks import InMemoryPushNotificationConfigStore
from a2a.types.a2a_pb2 import SendMessageRequest
from google.protobuf.json_format import ParseDict
from starlette.types import Receive, Scope, Send
from test_adapter_jira import JWKS_URL, TOKEN_URL, Clock, FakeEdge, IdP
from test_edge_app import EDGE_TOKEN, discovery_card
from test_edge_auth import AUDIENCE, ISSUER, RSA_KEY
from test_edge_jwks import jwk
from test_tasks_service import FakeOrchestrator, make_card

from golem.adapters.__main__ import adapter_of, build_mattermost_app
from golem.adapters.common import (
    NOTIFICATION_TOKEN_HEADER,
    ClientCredentials,
    push_token,
    subject_of_push_token,
)
from golem.adapters.mattermost import (
    PushTarget,
    command_token_valid,
    create_mattermost_adapter_app,
    message_id,
    push_subject,
    target_of_subject,
)
from golem.edge.__main__ import authenticator
from golem.edge.app import create_edge_app
from golem.edge.policy import ChainLimits, Registry
from golem.jwks import SigningKeys, fetch_jwks
from golem.metrics import Metrics
from golem.ratelimit import Limiter, Rate
from golem.settings import SettingsError, mattermost_adapter_settings
from golem.tasks.app import PushDelivery, create_listeners

COMMAND_TOKEN = "qzgakf1nx3yt9dr4n8585ihbxy"
BOT_TOKEN = "bot-access-token"
PUSH_SECRET = b"push-secret"
PUBLIC_URL = "https://mattermost-adapter.example.test"
MATTERMOST_URL = "https://chat.example.test"
TEAM = "wx4zz8t4ttgmtxqiwfohijayzc"
CHANNEL = "fukxanjgjbnp7ng383at53k1sy"
USER = "erj6qck3rfgtujs86w5r6rckzh"
TRIGGER = (
    "ZWZ5ZjRndzR4YmJxOHJlZWh4MXpkaHozbnI6ZXJqNnFjazNyZmd0dWpzODZ3NXI2cmNremg6MTY2MjA0MTY5Njg5Nj"
    "pNRVFDSUQ5cTZ3MkRHU1RaNjhyaDh1TGl1STlSVHh2R1czSXZ5aGVRYjhkWThuZnlBaUI2YnlPR2ZpWlczR1FmVkdI"
    "ODlreEp4MmlVT0UxMm9LMjlkZ1d0RC8xbjZRPT0="
)
AGENTS = frozenset({"discovery"})
TEAMS = frozenset({TEAM})


def command_form(**overrides: str) -> dict[str, str]:
    """The fields of a custom slash command request, as in Mattermost's own example
    (https://developers.mattermost.com/integrate/slash-commands/custom/)."""
    return {
        "channel_id": CHANNEL,
        "channel_name": "town-square",
        "command": "/golem",
        "response_url": "http://localhost:8066/hooks/commands/i11f6nnfgfyk8eg56x9omc6dpa",
        "team_domain": "team-awesome",
        "team_id": TEAM,
        "text": "discovery Redesign the checkout",
        "token": COMMAND_TOKEN,
        "trigger_id": TRIGGER,
        "user_id": USER,
        "user_name": "alan",
    } | overrides


def command_headers(token: str | None = COMMAND_TOKEN) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mattermost-Bot/1.1",
    }
    if token is not None:
        headers["Authorization"] = f"Token {token}"
    return headers


async def command(
    client: httpx.AsyncClient,
    form: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.post(
        "/mattermost/command",
        content=urlencode(form if form is not None else command_form()),
        headers=headers if headers is not None else command_headers(),
    )


@dataclass
class FakeMattermost:
    """``POST /api/v4/posts`` with a bot's access token as Bearer.

    Request fields and the 201 answer: the API reference's CreatePost
    (https://developers.mattermost.com/api-documentation/#/operations/CreatePost). A repeated
    ``pending_post_id`` returns the post already created instead of a second one, as the
    server's ``deduplicateCreatePost`` does for 30 seconds (server/channels/app/post.go).
    """

    posts: list[dict[str, Any]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    status_code: int = 201

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "POST" and request.url.path == "/api/v4/posts", request.url
        assert request.headers["authorization"] == f"Bearer {BOT_TOKEN}"
        if self.status_code != 201:
            return httpx.Response(self.status_code, json={"message": "unavailable"})
        body = json.loads(request.content)
        assert {"channel_id", "message"} <= set(body)
        pending = body.get("pending_post_id")
        for post in self.posts:
            if pending and post.get("pending_post_id") == pending:
                return httpx.Response(201, json=post)
        post = {"id": f"post-{len(self.posts) + 1}", "user_id": "bot-user", **body}
        self.posts.append(post)
        return httpx.Response(201, json=post)


@dataclass
class Adapter:
    client: httpx.AsyncClient
    edge: FakeEdge
    idp: IdP
    mattermost: FakeMattermost


def credentials(idp: IdP, clock: Clock | None = None) -> ClientCredentials:
    return ClientCredentials(
        httpx.AsyncClient(transport=httpx.MockTransport(idp.handle)),
        token_url=TOKEN_URL,
        client_id=idp.client_id,
        client_secret="client-secret",
        clock=clock or Clock(),
    )


def bot_client(mattermost: FakeMattermost) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(mattermost.handle),
        base_url=MATTERMOST_URL,
        headers={"Authorization": f"Bearer {BOT_TOKEN}"},
    )


def adapter_app(
    *,
    edge: httpx.AsyncClient,
    idp: IdP,
    mattermost: FakeMattermost,
    channels: frozenset[str] = frozenset(),
    start_timeout_seconds: float = 5.0,
    **options: Any,
) -> Any:
    return create_mattermost_adapter_app(
        command_token=COMMAND_TOKEN.encode(),
        agents=AGENTS,
        teams=TEAMS,
        channels=channels,
        push_secret=PUSH_SECRET,
        public_base_url=PUBLIC_URL,
        edge=edge,
        service_token=credentials(idp).token,
        mattermost=bot_client(mattermost),
        start_timeout_seconds=start_timeout_seconds,
        **options,
    )


def fake_edge_client(edge: FakeEdge) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(edge.handle), base_url="https://edge.example.test"
    )


@pytest.fixture
async def adapter() -> AsyncIterator[Adapter]:
    edge, idp, mattermost = FakeEdge(), IdP(client_id="mattermost-adapter"), FakeMattermost()
    app = adapter_app(edge=fake_edge_client(edge), idp=idp, mattermost=mattermost)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL
    ) as client:
        yield Adapter(client, edge, idp, mattermost)


def status_update(state: str, text: str = "", task_id: str = "task-1") -> dict[str, Any]:
    status: dict[str, Any] = {"state": state}
    if text:
        status["message"] = {"messageId": "m-out", "role": "ROLE_AGENT", "parts": [{"text": text}]}
    return {"statusUpdate": {"taskId": task_id, "contextId": "ctx-1", "status": status}}


def token_for(trigger: str = TRIGGER, user_name: str = "alan") -> str:
    message = message_id(trigger)
    target = PushTarget(channel_id=CHANNEL, user_id=USER, user_name=user_name, run=message)
    return push_token(PUSH_SECRET, push_subject(target), message)


async def push(
    client: httpx.AsyncClient, payload: dict[str, Any], token: str | None
) -> httpx.Response:
    headers = {} if token is None else {NOTIFICATION_TOKEN_HEADER: token}
    return await client.post("/a2a/push", json=payload, headers=headers)


def pushed_config(edge: FakeEdge) -> dict[str, str]:
    return edge.bodies()[0]["params"]["configuration"]["taskPushNotificationConfig"]


# The slash command's token


def test_the_documented_authorization_header_is_accepted() -> None:
    assert command_token_valid(COMMAND_TOKEN.encode(), f"Token {COMMAND_TOKEN}")


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Token",
        "Token ",
        f"Bearer {COMMAND_TOKEN}",
        COMMAND_TOKEN,
        f"Token {COMMAND_TOKEN}x",
        f"Token {COMMAND_TOKEN[:-1]}",
        "Token другой",
    ],
    ids=[
        "none",
        "empty",
        "scheme-only",
        "blank",
        "bearer",
        "bare",
        "longer",
        "shorter",
        "non-ascii",
    ],
)
def test_other_authorization_headers_are_refused(header: str | None) -> None:
    assert not command_token_valid(COMMAND_TOKEN.encode(), header)


async def test_a_command_without_the_token_is_refused(adapter: Adapter) -> None:
    response = await command(adapter.client, headers=command_headers(None))

    assert response.status_code == 401
    assert adapter.edge.calls == []
    assert adapter.idp.grants == []


async def test_a_command_with_a_wrong_token_is_refused(adapter: Adapter) -> None:
    response = await command(adapter.client, headers=command_headers("guessed"))

    assert response.status_code == 401
    assert adapter.edge.calls == []


async def test_the_token_in_the_form_alone_is_not_enough(adapter: Adapter) -> None:
    response = await command(adapter.client, headers=command_headers(None))

    assert "token" in command_form()
    assert response.status_code == 401


# The command to SendMessage


async def test_a_command_starts_the_agent_and_answers_privately_with_the_task_id(
    adapter: Adapter,
) -> None:
    response = await command(adapter.client)

    assert response.status_code == 200
    reply = response.json()
    assert reply["response_type"] == "ephemeral"
    assert "task-1" in reply["text"] and "discovery" in reply["text"]
    [request] = adapter.edge.calls
    assert request.url.path == "/a2a"
    assert request.headers["a2a-version"] == "1.0"
    assert request.headers["authorization"].startswith("Bearer ")
    params = json.loads(request.content)["params"]
    assert params["tenant"] == "discovery"
    assert params["message"]["messageId"] == message_id(TRIGGER)
    [part] = params["message"]["parts"]
    assert "Redesign the checkout" in part["text"]
    assert "@alan" in part["text"] and USER in part["text"]
    assert params["message"]["metadata"] == {
        "mattermost": {"userId": USER, "userName": "alan", "teamId": TEAM, "channelId": CHANNEL}
    }


async def test_the_push_config_names_the_channel_and_the_user(adapter: Adapter) -> None:
    await command(adapter.client)

    config = pushed_config(adapter.edge)
    assert config["url"] == f"{PUBLIC_URL}/a2a/push"
    subject = subject_of_push_token(PUSH_SECRET, config["token"])
    assert subject is not None
    assert target_of_subject(subject) == PushTarget(
        channel_id=CHANNEL, user_id=USER, user_name="alan", run=message_id(TRIGGER)
    )


async def test_the_message_is_a_valid_a2a_send_message_request(adapter: Adapter) -> None:
    await command(adapter.client)

    request = ParseDict(adapter.edge.bodies()[0]["params"], SendMessageRequest())

    assert request.tenant == "discovery"
    assert request.message.metadata["mattermost"]["userName"] == "alan"
    assert request.configuration.task_push_notification_config.token


async def test_a_retried_command_sends_the_same_message_id(adapter: Adapter) -> None:
    await command(adapter.client)
    await command(adapter.client)

    ids = {b["params"]["message"]["messageId"] for b in adapter.edge.bodies()}
    assert len(adapter.edge.calls) == 2
    assert len(ids) == 1


def test_each_invocation_gets_its_own_message_id() -> None:
    assert message_id(TRIGGER) == message_id(TRIGGER)
    assert message_id(TRIGGER) != message_id(TRIGGER[:-4] + "AAA=")
    assert message_id(TRIGGER).startswith("mattermost:")


async def test_commands_reuse_the_cached_service_token(adapter: Adapter) -> None:
    await command(adapter.client, command_form(trigger_id="t1"))
    await command(adapter.client, command_form(trigger_id="t2"))

    assert len(adapter.idp.grants) == 1
    assert len({c.headers["authorization"] for c in adapter.edge.calls}) == 1


@pytest.mark.parametrize(
    "text",
    ["", "   ", "discovery", "discovery   ", "unknown Redesign the checkout", "Discovery go"],
    ids=["empty", "blank", "no-goal", "blank-goal", "unknown-agent", "case-differs"],
)
async def test_a_bad_request_gets_a_private_usage_reply_and_no_run(
    adapter: Adapter, text: str
) -> None:
    response = await command(adapter.client, command_form(text=text))

    assert response.status_code == 200
    reply = response.json()
    assert reply["response_type"] == "ephemeral"
    assert "/golem <agent> <goal>" in reply["text"]
    assert "discovery" in reply["text"]
    assert adapter.edge.calls == []
    assert adapter.idp.grants == []


async def test_a_team_outside_the_allowlist_starts_nothing(adapter: Adapter) -> None:
    response = await command(adapter.client, command_form(team_id="otherteam0000000000000000a"))

    assert response.status_code == 200
    assert response.json()["response_type"] == "ephemeral"
    assert "not enabled" in response.json()["text"]
    assert adapter.edge.calls == []


async def test_a_channel_outside_a_set_allowlist_starts_nothing() -> None:
    edge = FakeEdge()
    app = adapter_app(
        edge=fake_edge_client(edge),
        idp=IdP(client_id="mattermost-adapter"),
        mattermost=FakeMattermost(),
        channels=frozenset({"allowedchannel00000000000a"}),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a") as c:
        refused = await command(c)
        allowed = await command(c, command_form(channel_id="allowedchannel00000000000a"))

    assert "not enabled" in refused.json()["text"]
    assert "task-1" in allowed.json()["text"]
    assert len(edge.calls) == 1


@pytest.mark.parametrize("missing", ["trigger_id", "user_id", "user_name", "channel_id", "team_id"])
async def test_a_command_missing_a_field_is_a_bad_request(adapter: Adapter, missing: str) -> None:
    form = command_form()
    del form[missing]

    response = await command(adapter.client, form)

    assert response.status_code == 400
    assert adapter.edge.calls == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("user_name", f"alan:{CHANNEL}:{USER}:bob"),
        ("user_name", "alan\nhere"),
        ("user_name", "@channel"),
        ("user_id", "erj6:x"),
        ("channel_id", "fukx.anj"),
        ("team_id", "wx4z z8t"),
    ],
)
async def test_ids_and_names_outside_mattermosts_alphabet_are_a_bad_request(
    adapter: Adapter, name: str, value: str
) -> None:
    response = await command(adapter.client, command_form(**{name: value}))

    assert response.status_code == 400
    assert adapter.edge.calls == []


async def test_the_goal_may_follow_the_agent_after_any_whitespace(adapter: Adapter) -> None:
    response = await command(adapter.client, command_form(text="discovery\tRedesign it"))

    assert "task-1" in response.json()["text"]
    assert "Redesign it" in adapter.edge.bodies()[0]["params"]["message"]["parts"][0]["text"]


async def test_an_unavailable_edge_is_told_to_the_user(adapter: Adapter) -> None:
    adapter.edge.status_code = 503

    response = await command(adapter.client)

    assert response.status_code == 200
    assert response.json()["response_type"] == "ephemeral"
    assert "could not start" in response.json()["text"].lower()


async def test_an_unavailable_identity_provider_is_told_to_the_user() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    edge = FakeEdge()
    app = create_mattermost_adapter_app(
        command_token=COMMAND_TOKEN.encode(),
        agents=AGENTS,
        teams=TEAMS,
        channels=frozenset(),
        push_secret=PUSH_SECRET,
        public_base_url=PUBLIC_URL,
        edge=fake_edge_client(edge),
        service_token=ClientCredentials(
            httpx.AsyncClient(transport=httpx.MockTransport(down)),
            token_url=TOKEN_URL,
            client_id="mattermost-adapter",
            client_secret="client-secret",
        ).token,
        mattermost=httpx.AsyncClient(transport=httpx.MockTransport(FakeMattermost().handle)),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a") as c:
        response = await command(c)

    assert "could not start" in response.json()["text"].lower()
    assert edge.calls == []


async def test_a_slow_start_is_answered_before_mattermost_gives_up() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(504)

    app = adapter_app(
        edge=httpx.AsyncClient(transport=httpx.MockTransport(slow), base_url="http://e"),
        idp=IdP(client_id="mattermost-adapter"),
        mattermost=FakeMattermost(),
        start_timeout_seconds=0.05,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a") as c:
        response = await asyncio.wait_for(command(c), timeout=2)

    assert response.status_code == 200
    assert response.json()["response_type"] == "ephemeral"
    assert "not confirmed" in response.json()["text"]


# Push tokens


def test_a_push_subject_round_trips_with_dots_in_the_user_name() -> None:
    target = PushTarget(CHANNEL, USER, "alan.turing-1_x", message_id(TRIGGER))
    token = push_token(PUSH_SECRET, push_subject(target), target.run)

    subject = subject_of_push_token(PUSH_SECRET, token)
    assert subject is not None
    assert target_of_subject(subject) == target


@pytest.mark.parametrize("subject", ["", "a:b", "mattermost:x:ch", "::::"])
def test_a_malformed_subject_names_no_target(subject: str) -> None:
    assert target_of_subject(subject) is None


async def test_a_push_without_a_token_is_refused(adapter: Adapter) -> None:
    response = await push(adapter.client, status_update("TASK_STATE_COMPLETED", "done"), None)

    assert response.status_code == 401
    assert adapter.mattermost.requests == []


@pytest.mark.parametrize(
    "token",
    [
        push_token(b"guessed", "mattermost:x:" + CHANNEL + ":" + USER + ":alan", "m"),
        token_for().replace(CHANNEL, "otherchannel000000000000a", 1),
        token_for()[:-1] + ("0" if token_for()[-1] != "0" else "1"),
        "garbage",
    ],
    ids=["other-secret", "other-channel", "altered-mac", "garbage"],
)
async def test_a_forged_push_token_is_refused(adapter: Adapter, token: str) -> None:
    response = await push(adapter.client, status_update("TASK_STATE_COMPLETED", "done"), token)

    assert response.status_code == 401
    assert adapter.mattermost.requests == []


# Push to a post


async def test_a_completed_run_posts_the_merge_request_once_mentioning_the_user(
    adapter: Adapter,
) -> None:
    mr = "https://gitlab.example.test/shop/context/-/merge_requests/12"

    response = await push(
        adapter.client, status_update("TASK_STATE_COMPLETED", f"Merge request: {mr}"), token_for()
    )

    assert response.status_code == 200
    [request] = adapter.mattermost.requests
    assert str(request.url) == f"{MATTERMOST_URL}/api/v4/posts"
    assert request.headers["content-type"] == "application/json"
    [post] = adapter.mattermost.posts
    assert post["channel_id"] == CHANNEL
    assert post["message"].startswith("@alan ")
    assert "completed" in post["message"] and mr in post["message"]
    assert "task-1" in post["message"]
    assert post["props"] == {"golem_task_id": "task-1"}


async def test_a_failed_run_posts_the_reason(adapter: Adapter) -> None:
    await push(
        adapter.client, status_update("TASK_STATE_FAILED", "lead found no target"), token_for()
    )

    [post] = adapter.mattermost.posts
    assert "failed" in post["message"] and "lead found no target" in post["message"]


async def test_the_runs_text_mentions_nobody_only_the_user_who_asked(adapter: Adapter) -> None:
    # The text carries the run's report, which an untrusted Job wrote.
    text = "@all urgent, @channel and @here: ask @mallory.b"
    await push(adapter.client, status_update("TASK_STATE_FAILED", text), token_for())

    [post] = adapter.mattermost.posts
    assert re.findall(r"@[A-Za-z0-9._-]+", post["message"]) == ["@alan"]
    assert "urgent" in post["message"]


@pytest.mark.parametrize(
    "state", ["TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_INPUT_REQUIRED"]
)
async def test_a_non_terminal_push_posts_nothing(adapter: Adapter, state: str) -> None:
    response = await push(adapter.client, status_update(state, "progress"), token_for())

    assert response.status_code == 204
    assert adapter.mattermost.requests == []


async def test_an_unreadable_push_is_a_bad_request(adapter: Adapter) -> None:
    response = await push(adapter.client, {"artifactUpdate": {"taskId": "t"}}, token_for())

    assert response.status_code == 400
    assert adapter.mattermost.requests == []


async def test_an_unavailable_mattermost_fails_the_push(adapter: Adapter) -> None:
    adapter.mattermost.status_code = 503

    response = await push(adapter.client, status_update("TASK_STATE_COMPLETED", "d"), token_for())

    assert response.status_code == 502


async def test_the_tasks_of_one_run_share_one_pending_post_id(adapter: Adapter) -> None:
    """A replayed command becomes a second task of the same run; both are told the outcome."""
    await push(adapter.client, status_update("TASK_STATE_COMPLETED", "d", "task-1"), token_for())
    await push(adapter.client, status_update("TASK_STATE_COMPLETED", "d", "task-2"), token_for())
    await push(
        adapter.client,
        status_update("TASK_STATE_COMPLETED", "d", "task-3"),
        token_for(trigger=TRIGGER[:-4] + "AAA="),
    )

    pending = [json.loads(r.content)["pending_post_id"] for r in adapter.mattermost.requests]
    assert pending[0] == pending[1] != pending[2]
    assert len(adapter.mattermost.posts) == 2


# Settings and process


MATTERMOST_ENV = {
    "GOLEM_EDGE_URL": "https://golem.example.test",
    "GOLEM_OIDC_TOKEN_URL": TOKEN_URL,
    "GOLEM_OIDC_CLIENT_ID": "mattermost-adapter",
    "GOLEM_OIDC_CLIENT_SECRET": "client-secret",
    "GOLEM_PUSH_TOKEN_SECRET": "push-secret",
    "GOLEM_PUBLIC_BASE_URL": PUBLIC_URL,
    "GOLEM_MATTERMOST_URL": MATTERMOST_URL,
    "GOLEM_MATTERMOST_BOT_TOKEN": BOT_TOKEN,
    "GOLEM_MATTERMOST_COMMAND_TOKEN": COMMAND_TOKEN,
    "GOLEM_MATTERMOST_AGENTS": "discovery, reviewer",
    "GOLEM_MATTERMOST_TEAMS": TEAM,
}


def test_mattermost_settings_are_parsed() -> None:
    settings = mattermost_adapter_settings(
        {**MATTERMOST_ENV, "GOLEM_MATTERMOST_CHANNELS": f"{CHANNEL},b", "GOLEM_PORT": "8090"}
    )

    assert settings.client_id == "mattermost-adapter"
    assert settings.mattermost_url == MATTERMOST_URL
    assert settings.command_token == COMMAND_TOKEN.encode()
    assert settings.push_secret == b"push-secret"
    assert settings.agents == frozenset({"discovery", "reviewer"})
    assert settings.teams == frozenset({TEAM})
    assert settings.channels == frozenset({CHANNEL, "b"})
    assert settings.port == 8090


def test_no_channel_list_allows_every_channel_of_the_allowed_teams() -> None:
    assert mattermost_adapter_settings(MATTERMOST_ENV).channels == frozenset()


def test_secrets_stay_out_of_the_settings_repr() -> None:
    text = repr(mattermost_adapter_settings(MATTERMOST_ENV))

    assert BOT_TOKEN not in text and COMMAND_TOKEN not in text and "client-secret" not in text


def test_every_missing_mattermost_variable_is_reported_at_once() -> None:
    with pytest.raises(SettingsError) as error:
        mattermost_adapter_settings({})

    for name in MATTERMOST_ENV:
        assert name in str(error.value)


@pytest.mark.parametrize("name", ["GOLEM_MATTERMOST_AGENTS", "GOLEM_MATTERMOST_TEAMS"])
def test_an_allowlist_of_only_separators_is_refused(name: str) -> None:
    with pytest.raises(SettingsError, match=name):
        mattermost_adapter_settings({**MATTERMOST_ENV, name: " , ,"})


def test_the_mattermost_url_must_not_end_with_a_slash() -> None:
    with pytest.raises(SettingsError, match="GOLEM_MATTERMOST_URL"):
        mattermost_adapter_settings(
            {**MATTERMOST_ENV, "GOLEM_MATTERMOST_URL": MATTERMOST_URL + "/"}
        )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [([], "jira"), (["jira"], "jira"), (["mattermost"], "mattermost")],
)
def test_the_adapter_is_chosen_by_its_argument_jira_by_default(
    argv: list[str], expected: str
) -> None:
    assert adapter_of(argv) == expected


@pytest.mark.parametrize("argv", [["slack"], ["jira", "mattermost"]])
def test_an_unknown_adapter_is_refused(argv: list[str]) -> None:
    with pytest.raises(SettingsError, match="jira, mattermost"):
        adapter_of(argv)


def test_the_process_builds_the_mattermost_adapter_from_settings() -> None:
    app = build_mattermost_app(mattermost_adapter_settings(MATTERMOST_ENV))

    assert {route.path for route in app.routes} == {"/mattermost/command", "/a2a/push"}


async def test_the_process_limits_commands_as_its_settings_say() -> None:
    settings = mattermost_adapter_settings(
        MATTERMOST_ENV | {"GOLEM_RATE_COMMAND_PER_MINUTE": "1", "GOLEM_RATE_COMMAND_BURST": "1"}
    )
    app = build_mattermost_app(settings)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL
    ) as client:
        first = await command(client, headers=command_headers("guessed"))
        second = await command(client, headers=command_headers("guessed"))

    assert (first.status_code, second.status_code) == (401, 429)


# End to end: adapter -> real edge -> real task service -> push -> adapter -> post


@dataclass
class Platform:
    adapter: httpx.AsyncClient
    tasks_public: httpx.AsyncClient
    tasks_write: httpx.AsyncClient
    orchestrator: FakeOrchestrator
    mattermost: FakeMattermost


@pytest.fixture
async def platform(audit_dsn: str, audit_admin_dsn: str) -> AsyncIterator[Platform]:
    idp = IdP(client_id="mattermost-adapter")
    jwks = {"keys": [jwk(RSA_KEY, "idp-key")]}

    def idp_handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) == JWKS_URL:
            return httpx.Response(200, json=jwks)
        return idp.handle(request)

    keys = SigningKeys(
        partial(fetch_jwks, httpx.Client(transport=httpx.MockTransport(idp_handle)), JWKS_URL)
    )
    keys.refresh()

    # The task service pushes to the adapter, which calls the edge, which calls the task service.
    adapter: dict[str, Any] = {}

    async def adapter_asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await adapter["app"](scope, receive, send)

    orchestrator = FakeOrchestrator()
    listeners = create_listeners(
        make_card(),
        orchestrator,
        edge_token=EDGE_TOKEN,
        push=PushDelivery(
            config_store=InMemoryPushNotificationConfigStore(),
            client=httpx.AsyncClient(transport=httpx.ASGITransport(app=adapter_asgi)),
            allowed_prefixes=(f"{PUBLIC_URL}/",),
        ),
    )
    edge_app = create_edge_app(
        authenticate=authenticator(keys, issuer=ISSUER, audience=AUDIENCE),
        registry=Registry(allowed_callers={"discovery": frozenset({"service:mattermost-adapter"})}),
        limits=ChainLimits(max_depth=3),
        audit_dsn=audit_dsn,
        forward=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=listeners.public), base_url="http://tasks"
        ),
        edge_token=EDGE_TOKEN,
        cards={"discovery": discovery_card()},
    )
    mattermost = FakeMattermost()
    adapter["app"] = create_mattermost_adapter_app(
        command_token=COMMAND_TOKEN.encode(),
        agents=AGENTS,
        teams=TEAMS,
        channels=frozenset(),
        push_secret=PUSH_SECRET,
        public_base_url=PUBLIC_URL,
        edge=httpx.AsyncClient(transport=httpx.ASGITransport(app=edge_app), base_url="http://edge"),
        service_token=ClientCredentials(
            httpx.AsyncClient(transport=httpx.MockTransport(idp_handle)),
            token_url=TOKEN_URL,
            client_id="mattermost-adapter",
            client_secret="client-secret",
        ).token,
        mattermost=bot_client(mattermost),
    )
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=adapter_asgi), base_url=PUBLIC_URL
        ) as a,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=listeners.public), base_url="http://tasks"
        ) as public,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=listeners.internal_write), base_url="http://tasks"
        ) as write,
    ):
        yield Platform(a, public, write, orchestrator, mattermost)


async def task_as_stored(platform: Platform, task_id: str) -> dict[str, Any]:
    response = await platform.tasks_public.post(
        "/a2a",
        headers={
            "A2A-Version": "1.0",
            "X-Golem-Edge-Token": EDGE_TOKEN,
            "X-Golem-Principal": "service:mattermost-adapter",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "GetTask",
            "params": {"tenant": "discovery", "id": task_id},
        },
    )
    return response.json()["result"]


async def test_a_command_starts_a_task_through_the_real_edge_as_the_service(
    platform: Platform,
) -> None:
    response = await command(platform.adapter)

    assert response.status_code == 200, response.text
    [run] = platform.orchestrator.started
    assert run.caller == "service:mattermost-adapter"
    assert run.agent == "discovery"
    assert run.message_id == message_id(TRIGGER)
    assert "Redesign the checkout" in run.goal and "@alan" in run.goal and USER in run.goal
    assert run.task_id in response.json()["text"]
    task = await task_as_stored(platform, run.task_id)
    [asked] = [m for m in task["history"] if m["role"] == "ROLE_USER"]
    assert asked["metadata"]["mattermost"] == {
        "userId": USER,
        "userName": "alan",
        "teamId": TEAM,
        "channelId": CHANNEL,
    }


async def test_the_outcome_reaches_the_channel_once_however_often_it_is_reported(
    platform: Platform,
) -> None:
    await command(platform.adapter)
    [run] = platform.orchestrator.started
    platform.orchestrator.finish(
        run.task_id,
        succeeded=True,
        detail="Merge request: https://gitlab.example.test/p/-/merge_requests/3",
    )
    outcome = {"task_id": run.task_id, "run_id": "run-1"}

    for _ in range(2):
        response = await platform.tasks_write.post("/internal/run-outcome", json=outcome)
        assert response.status_code == 200, response.text

    [post] = platform.mattermost.posts
    assert post["channel_id"] == CHANNEL
    assert post["message"].startswith("@alan ")
    assert "completed" in post["message"] and "merge_requests/3" in post["message"]
    assert len(platform.mattermost.requests) == 1


class FrozenClock:
    def __call__(self) -> float:
        return 0.0


async def test_commands_are_limited_per_address_before_the_token_check() -> None:
    edge, idp, mattermost = FakeEdge(), IdP(client_id="mattermost-adapter"), FakeMattermost()
    inbound = Limiter(Rate(per_minute=60, burst=2), clock=FrozenClock())
    app = adapter_app(edge=fake_edge_client(edge), idp=idp, mattermost=mattermost, inbound=inbound)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.5", 4000)), base_url=PUBLIC_URL
    ) as client:
        guessed = [await command(client, headers=command_headers("guessed")) for _ in range(2)]
        flooded = await command(client, headers=command_headers("guessed"))
        valid_but_late = await command(client)

    assert [r.status_code for r in guessed] == [401, 401]
    assert flooded.status_code == 429 and int(flooded.headers["retry-after"]) >= 1
    assert valid_but_late.status_code == 429
    assert edge.calls == []


# --- Metrics (ADR 0013) --------------------------------------------------------------------------


async def test_refused_commands_and_pushes_are_counted() -> None:
    edge, idp, mattermost = FakeEdge(), IdP(client_id="mattermost-adapter"), FakeMattermost()
    metrics = Metrics("mattermost-adapter")
    inbound = Limiter(Rate(per_minute=60, burst=2), clock=FrozenClock())
    app = adapter_app(
        edge=fake_edge_client(edge),
        idp=idp,
        mattermost=mattermost,
        inbound=inbound,
        metrics=metrics,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.5", 4000)), base_url=PUBLIC_URL
    ) as client:
        for _ in range(3):
            await command(client, headers=command_headers("guessed"))
        await client.post("/a2a/push", json={}, headers={"X-A2A-Notification-Token": "forged"})

    value = metrics.registry.get_sample_value
    process = {"process": "mattermost-adapter"}
    assert value("golem_authentication_failures_total", process) == 3
    assert value("golem_rate_limit_refusals_total", process | {"limit": "command"}) == 1
