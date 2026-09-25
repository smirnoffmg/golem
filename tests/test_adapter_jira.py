import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from a2a.types.a2a_pb2 import SendMessageRequest
from google.protobuf.json_format import ParseDict
from test_edge_app import EDGE_TOKEN, TaskService, discovery_card
from test_edge_auth import AUDIENCE, ISSUER, RSA_KEY, claims
from test_edge_jwks import jwk

from golem.adapters.__main__ import build_app
from golem.adapters.common import (
    NOTIFICATION_TOKEN_HEADER,
    ClientCredentials,
    push_token,
)
from golem.adapters.common import subject_of_push_token as issue_of_push_token
from golem.adapters.jira import (
    create_jira_adapter_app,
    jira_authorization,
    labels_added,
    message_id,
    signature_valid,
)
from golem.edge.__main__ import authenticator
from golem.edge.app import create_edge_app
from golem.edge.policy import ChainLimits, Registry
from golem.jwks import SigningKeys, fetch_jwks
from golem.ratelimit import Limiter, Rate, parse_networks
from golem.settings import SettingsError, adapter_settings, parse_label_agents

WEBHOOK_SECRET = b"webhook-secret"
PUSH_SECRET = b"push-secret"
PUBLIC_URL = "https://jira-adapter.example.test"
TOKEN_URL = "https://idp.example.test/realms/golem/protocol/openid-connect/token"
JWKS_URL = "https://idp.example.test/realms/golem/protocol/openid-connect/certs"
JIRA_URL = "https://jira.example.test"
LABELS = {"golem:discovery": "discovery"}
TIMESTAMP = 1606480436302


def issue_updated(
    *,
    key: str = "SHOP-7",
    summary: str = "Redesign the checkout",
    field_name: str = "labels",
    before: str | None = "frontend",
    after: str | None = "frontend golem:discovery",
    timestamp: int = TIMESTAMP,
) -> dict[str, Any]:
    """The shape of Jira's ``jira:issue_updated`` webhook body (Cloud webhooks guide)."""
    return {
        "timestamp": timestamp,
        "webhookEvent": "jira:issue_updated",
        "issue_event_type_name": "issue_updated",
        "user": {"accountId": "99:27935d01", "displayName": "Alice"},
        "issue": {"id": "99291", "key": key, "fields": {"summary": summary}},
        "changelog": {
            "id": "10500",
            "items": [
                {
                    "field": field_name,
                    "fieldtype": "jira",
                    "from": None,
                    "fromString": before,
                    "to": None,
                    "toString": after,
                }
            ],
        },
    }


def signed(body: bytes, secret: bytes = WEBHOOK_SECRET) -> dict[str, str]:
    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature": f"sha256={digest}", "Content-Type": "application/json"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class IdP:
    """Keycloak's token endpoint for the client credentials grant."""

    expires_in: int = 300
    client_id: str = "jira-adapter"
    grants: list[dict[str, list[str]]] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TOKEN_URL
        assert request.method == "POST"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        self.grants.append(parse_qs(request.content.decode()))
        now = int(time.time())
        token = jwt.encode(
            claims(
                preferred_username=f"service-account-{self.client_id}",
                azp=self.client_id,
                sub=f"grant-{len(self.grants)}",
                iat=now,
                nbf=now,
                exp=now + self.expires_in,
            ),
            RSA_KEY,
            algorithm="RS256",
            headers={"kid": "idp-key"},
        )
        return httpx.Response(
            200,
            json={
                "access_token": token,
                "expires_in": self.expires_in,
                "token_type": "Bearer",
                "scope": "profile email",
            },
        )


@dataclass
class FakeEdge:
    """The edge as seen by the adapter: a JSON-RPC endpoint that records every call."""

    calls: list[httpx.Request] = field(default_factory=list)
    status_code: int = 200

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "task": {
                        "id": f"task-{len(self.calls)}",
                        "contextId": "ctx-1",
                        "status": {"state": "TASK_STATE_WORKING"},
                    }
                },
            },
        )

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(call.content) for call in self.calls]


@dataclass
class FakeJira:
    """Jira REST API v2 issue comments: GET pages of comments, POST a comment."""

    comments: dict[str, list[str]] = field(default_factory=dict)
    page_size: int = 2
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        prefix, _, rest = request.url.path.partition("/rest/api/2/issue/")
        key, _, tail = rest.partition("/")
        assert prefix == "" and tail == "comment", request.url
        bodies = self.comments.setdefault(key, [])
        if request.method == "POST":
            bodies.append(json.loads(request.content)["body"])
            return httpx.Response(201, json={"id": str(10000 + len(bodies)), "body": bodies[-1]})
        start = int(request.url.params.get("startAt", "0"))
        page = bodies[start : start + self.page_size]
        return httpx.Response(
            200,
            json={
                "startAt": start,
                "maxResults": self.page_size,
                "total": len(bodies),
                "comments": [
                    {"id": str(10001 + start + i), "body": body} for i, body in enumerate(page)
                ],
            },
        )

    def posted(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]


@dataclass
class Adapter:
    client: httpx.AsyncClient
    edge: FakeEdge
    idp: IdP
    jira: FakeJira
    clock: Clock


def credentials(idp: IdP, clock: Clock) -> ClientCredentials:
    return ClientCredentials(
        httpx.AsyncClient(transport=httpx.MockTransport(idp.handle)),
        token_url=TOKEN_URL,
        client_id="jira-adapter",
        client_secret="client-secret",
        clock=clock,
    )


@pytest.fixture
async def adapter() -> AsyncIterator[Adapter]:
    edge, idp, jira, clock = FakeEdge(), IdP(), FakeJira(), Clock()
    app = create_jira_adapter_app(
        labels=LABELS,
        webhook_secret=WEBHOOK_SECRET,
        push_secret=PUSH_SECRET,
        public_base_url=PUBLIC_URL,
        edge=httpx.AsyncClient(
            transport=httpx.MockTransport(edge.handle), base_url="https://edge.example.test"
        ),
        service_token=credentials(idp, clock).token,
        jira=httpx.AsyncClient(transport=httpx.MockTransport(jira.handle), base_url=JIRA_URL),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL
    ) as client:
        yield Adapter(client, edge, idp, jira, clock)


async def deliver(client: httpx.AsyncClient, payload: dict[str, Any]) -> httpx.Response:
    body = json.dumps(payload).encode()
    return await client.post("/jira/webhook", content=body, headers=signed(body))


def status_update(
    state: str, text: str = "", task_id: str = "task-1", context_id: str = "ctx-1"
) -> dict[str, Any]:
    status: dict[str, Any] = {"state": state}
    if text:
        status["message"] = {
            "messageId": "m-out",
            "role": "ROLE_AGENT",
            "parts": [{"text": text}],
        }
    return {"statusUpdate": {"taskId": task_id, "contextId": context_id, "status": status}}


async def push(
    client: httpx.AsyncClient, payload: dict[str, Any], token: str | None
) -> httpx.Response:
    headers = {} if token is None else {NOTIFICATION_TOKEN_HEADER: token}
    return await client.post("/a2a/push", json=payload, headers=headers)


def token_for(issue_key: str = "SHOP-7") -> str:
    return push_token(PUSH_SECRET, issue_key, message_id(issue_key, "golem:discovery", "1"))


# Signature


def test_signature_matches_the_example_in_atlassians_webhook_guide() -> None:
    header = "sha256=a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace1f2937e133263c9"

    assert signature_valid(b"It's a Secret to Everybody", b"Hello World!", header)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "sha256=",
        "sha256=00",
        "a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace1f2937e133263c9",
        "sha1=a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace1f2937e133263c9",
        "md5=a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace1f2937e133263c9",
    ],
)
def test_missing_malformed_or_unsupported_signatures_are_invalid(header: str | None) -> None:
    assert not signature_valid(b"It's a Secret to Everybody", b"Hello World!", header)


def test_signature_with_another_secret_is_invalid() -> None:
    body = b"Hello World!"

    assert not signature_valid(WEBHOOK_SECRET, body, signed(body, b"other")["X-Hub-Signature"])


async def test_unsigned_webhook_is_refused(adapter: Adapter) -> None:
    response = await adapter.client.post("/jira/webhook", json=issue_updated())

    assert response.status_code == 401
    assert adapter.edge.calls == []


async def test_badly_signed_webhook_is_refused(adapter: Adapter) -> None:
    body = json.dumps(issue_updated()).encode()
    headers = signed(body, secret=b"guessed")

    response = await adapter.client.post("/jira/webhook", content=body, headers=headers)

    assert response.status_code == 401
    assert adapter.edge.calls == []


async def test_body_changed_after_signing_is_refused(adapter: Adapter) -> None:
    body = json.dumps(issue_updated()).encode()
    headers = signed(body)
    tampered = json.dumps(issue_updated(key="PAY-1")).encode()

    response = await adapter.client.post("/jira/webhook", content=tampered, headers=headers)

    assert response.status_code == 401
    assert adapter.edge.calls == []


# Parsing the webhook


def test_added_label_is_found_in_the_changelog() -> None:
    [added] = labels_added(issue_updated())

    assert (added.issue_key, added.summary, added.label, added.event) == (
        "SHOP-7",
        "Redesign the checkout",
        "golem:discovery",
        str(TIMESTAMP),
    )


@pytest.mark.parametrize(
    "payload",
    [
        issue_updated(before="frontend golem:discovery", after="frontend"),
        issue_updated(before="golem:discovery", after=None),
        issue_updated(before="golem:discovery", after="golem:discovery"),
        issue_updated(field_name="summary", before="Old", after="golem:discovery"),
        {**issue_updated(), "webhookEvent": "jira:issue_created"},
        {**issue_updated(), "changelog": None},
        {"timestamp": TIMESTAMP},
        [],
    ],
    ids=[
        "removed",
        "removed-last",
        "unchanged",
        "other-field",
        "other-event",
        "no-changelog",
        "no-issue",
        "not-an-object",
    ],
)
def test_nothing_is_added_unless_a_label_appears(payload: Any) -> None:
    assert labels_added(payload) == []


def test_first_label_on_an_issue_is_added() -> None:
    assert [a.label for a in labels_added(issue_updated(before=None, after="golem:discovery"))] == [
        "golem:discovery"
    ]


def test_message_id_is_deterministic_per_issue_label_and_event() -> None:
    assert message_id("SHOP-7", "golem:discovery", "1") == message_id(
        "SHOP-7", "golem:discovery", "1"
    )
    assert (
        len(
            {
                message_id("SHOP-7", "golem:discovery", "1"),
                message_id("SHOP-8", "golem:discovery", "1"),
                message_id("SHOP-7", "golem:review", "1"),
                message_id("SHOP-7", "golem:discovery", "2"),
            }
        )
        == 4
    )


# Webhook to SendMessage


async def test_mapped_label_sends_one_message_to_the_agent(adapter: Adapter) -> None:
    response = await deliver(adapter.client, issue_updated())

    assert response.status_code == 202
    assert response.json() == {"tasks": ["task-1"]}
    [request] = adapter.edge.calls
    assert request.url.path == "/a2a"
    assert request.headers["a2a-version"] == "1.0"
    assert request.headers["authorization"].startswith("Bearer ")
    body = json.loads(request.content)
    assert (body["jsonrpc"], body["method"]) == ("2.0", "SendMessage")
    params = body["params"]
    assert params["tenant"] == "discovery"
    expected_id = message_id("SHOP-7", "golem:discovery", str(TIMESTAMP))
    assert params["message"]["messageId"] == expected_id
    assert params["message"]["role"] == "ROLE_USER"
    [part] = params["message"]["parts"]
    assert "SHOP-7" in part["text"] and "Redesign the checkout" in part["text"]
    config = params["configuration"]["taskPushNotificationConfig"]
    assert config["url"] == f"{PUBLIC_URL}/a2a/push"
    assert issue_of_push_token(PUSH_SECRET, config["token"]) == "SHOP-7"


async def test_message_is_a_valid_a2a_send_message_request(adapter: Adapter) -> None:
    await deliver(adapter.client, issue_updated())

    request = ParseDict(adapter.edge.bodies()[0]["params"], SendMessageRequest())

    assert request.tenant == "discovery"
    push_config = request.configuration.task_push_notification_config
    assert push_config.url == f"{PUBLIC_URL}/a2a/push"
    assert push_config.token


async def test_each_run_gets_its_own_push_token(adapter: Adapter) -> None:
    await deliver(adapter.client, issue_updated(timestamp=1))
    await deliver(adapter.client, issue_updated(timestamp=2))

    tokens = [
        b["params"]["configuration"]["taskPushNotificationConfig"]["token"]
        for b in adapter.edge.bodies()
    ]
    assert len(set(tokens)) == 2


async def test_webhook_retry_sends_the_same_message_id(adapter: Adapter) -> None:
    body = json.dumps(issue_updated()).encode()
    for retry in ("1", "2"):
        headers = {**signed(body), "X-Atlassian-Webhook-Retry": retry}
        await adapter.client.post("/jira/webhook", content=body, headers=headers)

    ids = {b["params"]["message"]["messageId"] for b in adapter.edge.bodies()}
    assert len(adapter.edge.calls) == 2
    assert len(ids) == 1


@pytest.mark.parametrize(
    "payload",
    [
        issue_updated(after="frontend golem:unknown"),
        issue_updated(before="frontend golem:discovery", after="frontend"),
        issue_updated(field_name="summary"),
    ],
    ids=["unmapped-label", "label-removed", "other-field"],
)
async def test_unmapped_or_removed_labels_send_nothing(adapter: Adapter, payload: Any) -> None:
    response = await deliver(adapter.client, payload)

    assert response.status_code == 204
    assert adapter.edge.calls == []
    assert adapter.idp.grants == []


async def test_signed_body_that_is_not_json_is_a_bad_request(adapter: Adapter) -> None:
    body = b"{not json"

    response = await adapter.client.post("/jira/webhook", content=body, headers=signed(body))

    assert response.status_code == 400
    assert adapter.edge.calls == []


async def test_unavailable_edge_asks_jira_to_retry(adapter: Adapter) -> None:
    adapter.edge.status_code = 503

    response = await deliver(adapter.client, issue_updated())

    assert response.status_code == 502


# Service token


async def test_service_token_uses_the_client_credentials_grant() -> None:
    idp, clock = IdP(), Clock()

    token = await credentials(idp, clock).token()

    assert token
    assert idp.grants == [
        {
            "grant_type": ["client_credentials"],
            "client_id": ["jira-adapter"],
            "client_secret": ["client-secret"],
        }
    ]


async def test_service_token_is_cached_until_shortly_before_expiry() -> None:
    idp, clock = IdP(expires_in=300), Clock()
    source = credentials(idp, clock)

    first = await source.token()
    clock.now += 200
    assert await source.token() == first
    assert len(idp.grants) == 1

    clock.now += 80
    assert await source.token() != first
    assert len(idp.grants) == 2


async def test_webhooks_reuse_the_cached_token(adapter: Adapter) -> None:
    await deliver(adapter.client, issue_updated(timestamp=1))
    await deliver(adapter.client, issue_updated(timestamp=2))

    assert len(adapter.idp.grants) == 1
    assert len({c.headers["authorization"] for c in adapter.edge.calls}) == 1


async def test_unavailable_identity_provider_asks_jira_to_retry() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    source = ClientCredentials(
        httpx.AsyncClient(transport=httpx.MockTransport(down)),
        token_url=TOKEN_URL,
        client_id="jira-adapter",
        client_secret="client-secret",
    )
    edge = FakeEdge()
    app = create_jira_adapter_app(
        labels=LABELS,
        webhook_secret=WEBHOOK_SECRET,
        push_secret=PUSH_SECRET,
        public_base_url=PUBLIC_URL,
        edge=httpx.AsyncClient(transport=httpx.MockTransport(edge.handle), base_url="http://e"),
        service_token=source.token,
        jira=httpx.AsyncClient(transport=httpx.MockTransport(FakeJira().handle)),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a") as c:
        response = await deliver(c, issue_updated())

    assert response.status_code == 502
    assert edge.calls == []


# Push tokens


def test_push_token_names_its_issue() -> None:
    assert issue_of_push_token(PUSH_SECRET, token_for("SHOP-7")) == "SHOP-7"


@pytest.mark.parametrize(
    "token",
    [
        "",
        "SHOP-7",
        "SHOP-7.nonce",
        "SHOP-8." + token_for("SHOP-7").split(".", 1)[1],
        token_for("SHOP-7")[:-1] + ("0" if token_for("SHOP-7")[-1] != "0" else "1"),
        push_token(b"other-secret", "SHOP-7", "m1"),
    ],
    ids=["empty", "no-mac", "no-mac-2", "other-issue", "altered-mac", "other-secret"],
)
def test_forged_push_tokens_name_no_issue(token: str) -> None:
    assert issue_of_push_token(PUSH_SECRET, token) is None


# Push to comment


async def test_push_without_a_token_is_refused(adapter: Adapter) -> None:
    response = await push(adapter.client, status_update("TASK_STATE_COMPLETED", "done"), None)

    assert response.status_code == 401
    assert adapter.jira.requests == []


async def test_push_with_a_bad_token_is_refused(adapter: Adapter) -> None:
    forged = push_token(b"guessed", "SHOP-7", "m1")

    response = await push(adapter.client, status_update("TASK_STATE_COMPLETED", "done"), forged)

    assert response.status_code == 401
    assert adapter.jira.requests == []


async def test_completed_push_comments_the_merge_request_url(adapter: Adapter) -> None:
    mr = "https://gitlab.example.test/shop/context/-/merge_requests/12"

    response = await push(
        adapter.client, status_update("TASK_STATE_COMPLETED", f"Merge request: {mr}"), token_for()
    )

    assert response.status_code == 200
    [comment] = adapter.jira.comments["SHOP-7"]
    assert mr in comment
    assert "completed" in comment
    assert "task-1" in comment


async def test_failed_push_comments_the_reason(adapter: Adapter) -> None:
    await push(
        adapter.client, status_update("TASK_STATE_FAILED", "lead found no target"), token_for()
    )

    [comment] = adapter.jira.comments["SHOP-7"]
    assert "failed" in comment
    assert "lead found no target" in comment


async def test_a_whole_task_push_is_understood_too(adapter: Adapter) -> None:
    task = {
        "task": {
            "id": "task-9",
            "contextId": "ctx-9",
            "status": {
                "state": "TASK_STATE_REJECTED",
                "message": {"messageId": "m", "role": "ROLE_AGENT", "parts": [{"text": "quota"}]},
            },
        }
    }

    await push(adapter.client, task, token_for())

    [comment] = adapter.jira.comments["SHOP-7"]
    assert "rejected" in comment and "quota" in comment and "task-9" in comment


async def test_comment_is_posted_to_the_v2_comment_endpoint(adapter: Adapter) -> None:
    await push(adapter.client, status_update("TASK_STATE_COMPLETED", "done"), token_for())

    [post] = adapter.jira.posted()
    assert str(post.url) == f"{JIRA_URL}/rest/api/2/issue/SHOP-7/comment"
    assert post.headers["content-type"] == "application/json"
    assert set(json.loads(post.content)) == {"body"}


async def test_duplicate_terminal_push_leaves_one_comment(adapter: Adapter) -> None:
    update = status_update("TASK_STATE_COMPLETED", "done")

    first = await push(adapter.client, update, token_for())
    second = await push(adapter.client, update, token_for())

    assert (first.status_code, second.status_code) == (200, 200)
    assert len(adapter.jira.comments["SHOP-7"]) == 1
    assert len(adapter.jira.posted()) == 1


async def test_duplicate_is_found_beyond_the_first_page_of_comments(adapter: Adapter) -> None:
    update = status_update("TASK_STATE_COMPLETED", "done")
    await push(adapter.client, update, token_for())
    adapter.jira.comments["SHOP-7"][:0] = ["older 1", "older 2", "older 3"]
    adapter.jira.comments["SHOP-7"].extend(["newer 1", "newer 2"])

    await push(adapter.client, update, token_for())

    assert len(adapter.jira.posted()) == 1


async def test_other_tasks_comments_do_not_count_as_duplicates(adapter: Adapter) -> None:
    await push(adapter.client, status_update("TASK_STATE_COMPLETED", "a", "task-1"), token_for())
    await push(adapter.client, status_update("TASK_STATE_COMPLETED", "b", "task-10"), token_for())

    assert len(adapter.jira.comments["SHOP-7"]) == 2


@pytest.mark.parametrize(
    "state", ["TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_INPUT_REQUIRED"]
)
async def test_non_terminal_push_is_acknowledged_without_a_comment(
    adapter: Adapter, state: str
) -> None:
    response = await push(adapter.client, status_update(state, "progress"), token_for())

    assert response.status_code == 204
    assert adapter.jira.requests == []


async def test_unreadable_push_is_a_bad_request(adapter: Adapter) -> None:
    response = await push(adapter.client, {"artifactUpdate": {"taskId": "t"}}, token_for())

    assert response.status_code == 400
    assert adapter.jira.requests == []


async def test_unavailable_jira_fails_the_push(adapter: Adapter) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    app = create_jira_adapter_app(
        labels=LABELS,
        webhook_secret=WEBHOOK_SECRET,
        push_secret=PUSH_SECRET,
        public_base_url=PUBLIC_URL,
        edge=httpx.AsyncClient(transport=httpx.MockTransport(FakeEdge().handle)),
        service_token=credentials(IdP(), Clock()).token,
        jira=httpx.AsyncClient(transport=httpx.MockTransport(down), base_url=JIRA_URL),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a") as c:
        response = await push(c, status_update("TASK_STATE_COMPLETED", "done"), token_for())

    assert response.status_code == 502


# Jira authentication


def test_jira_api_token_uses_basic_auth_with_the_account_email() -> None:
    header = jira_authorization(user="bot@example.test", token="api-token")

    assert header == "Basic " + base64.b64encode(b"bot@example.test:api-token").decode()


def test_jira_personal_access_token_is_a_bearer_token() -> None:
    assert jira_authorization(user=None, token="pat") == "Bearer pat"


# Settings


ADAPTER_ENV = {
    "GOLEM_EDGE_URL": "https://golem.example.test",
    "GOLEM_OIDC_TOKEN_URL": TOKEN_URL,
    "GOLEM_OIDC_CLIENT_ID": "jira-adapter",
    "GOLEM_OIDC_CLIENT_SECRET": "client-secret",
    "GOLEM_JIRA_URL": JIRA_URL,
    "GOLEM_JIRA_TOKEN": "pat",
    "GOLEM_JIRA_WEBHOOK_SECRET": "webhook-secret",
    "GOLEM_PUSH_TOKEN_SECRET": "push-secret",
    "GOLEM_JIRA_LABELS_FILE": "/etc/golem/jira-labels.yaml",
    "GOLEM_PUBLIC_BASE_URL": PUBLIC_URL,
}


def test_adapter_settings_are_parsed() -> None:
    settings = adapter_settings({**ADAPTER_ENV, "GOLEM_PORT": "8090"})

    assert settings.edge_url == "https://golem.example.test"
    assert settings.client_id == "jira-adapter"
    assert settings.jira_user is None
    assert settings.jira_token == "pat"
    assert settings.webhook_secret == b"webhook-secret"
    assert settings.push_secret == b"push-secret"
    assert str(settings.labels_file) == "/etc/golem/jira-labels.yaml"
    assert settings.port == 8090


def test_jira_user_selects_basic_auth() -> None:
    settings = adapter_settings({**ADAPTER_ENV, "GOLEM_JIRA_USER": "bot@example.test"})

    assert settings.jira_user == "bot@example.test"


def test_every_missing_adapter_variable_is_reported_at_once() -> None:
    with pytest.raises(SettingsError) as error:
        adapter_settings({})

    for name in ADAPTER_ENV:
        assert name in str(error.value)


def test_adapter_urls_must_not_end_with_a_slash() -> None:
    with pytest.raises(SettingsError, match="GOLEM_JIRA_URL"):
        adapter_settings({**ADAPTER_ENV, "GOLEM_JIRA_URL": f"{JIRA_URL}/"})


def test_label_mapping_is_parsed() -> None:
    assert parse_label_agents("golem:discovery: discovery\ngolem:review: reviewer\n") == {
        "golem:discovery": "discovery",
        "golem:review": "reviewer",
    }


@pytest.mark.parametrize(
    "text", ["- golem:discovery", "golem:discovery: [discovery]", "golem:discovery: ''"]
)
def test_label_mapping_must_map_labels_to_agent_names(text: str) -> None:
    with pytest.raises(SettingsError, match="labels"):
        parse_label_agents(text)


# End to end: adapter -> real edge -> real task service


async def test_label_starts_a_task_through_the_real_edge_as_the_service_account(
    audit_dsn: str, audit_admin_dsn: str
) -> None:
    idp = IdP()
    jwks = {"keys": [jwk(RSA_KEY, "idp-key")]}

    def idp_handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) == JWKS_URL:
            return httpx.Response(200, json=jwks)
        return idp.handle(request)

    keys = SigningKeys(
        partial(fetch_jwks, httpx.Client(transport=httpx.MockTransport(idp_handle)), JWKS_URL)
    )
    keys.refresh()
    tasks = TaskService()
    edge_app = create_edge_app(
        authenticate=authenticator(keys, issuer=ISSUER, audience=AUDIENCE),
        registry=Registry(allowed_callers={"discovery": frozenset({"service:jira-adapter"})}),
        limits=ChainLimits(max_depth=3),
        audit_dsn=audit_dsn,
        forward=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=tasks.app()), base_url="http://tasks"
        ),
        edge_token=EDGE_TOKEN,
        cards={"discovery": discovery_card()},
    )
    adapter_app = create_jira_adapter_app(
        labels=LABELS,
        webhook_secret=WEBHOOK_SECRET,
        push_secret=PUSH_SECRET,
        public_base_url=PUBLIC_URL,
        edge=httpx.AsyncClient(transport=httpx.ASGITransport(app=edge_app), base_url="http://edge"),
        service_token=ClientCredentials(
            httpx.AsyncClient(transport=httpx.MockTransport(idp_handle)),
            token_url=TOKEN_URL,
            client_id="jira-adapter",
            client_secret="client-secret",
        ).token,
        jira=httpx.AsyncClient(transport=httpx.MockTransport(FakeJira().handle)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=adapter_app), base_url=PUBLIC_URL
    ) as client:
        response = await deliver(client, issue_updated())

    assert response.status_code == 202, response.text
    [run] = tasks.orchestrator.started
    assert run.caller == "service:jira-adapter"
    assert run.agent == "discovery"
    assert run.message_id == message_id("SHOP-7", "golem:discovery", str(TIMESTAMP))
    assert "SHOP-7" in run.goal and "Redesign the checkout" in run.goal
    assert response.json() == {"tasks": [run.task_id]}


def test_process_builds_the_adapter_from_settings(tmp_path: Path) -> None:
    labels = tmp_path / "labels.yaml"
    labels.write_text("golem:discovery: discovery\n")
    settings = adapter_settings({**ADAPTER_ENV, "GOLEM_JIRA_LABELS_FILE": str(labels)})

    app = build_app(settings)

    assert {route.path for route in app.routes} == {"/jira/webhook", "/a2a/push"}


# --- Rate limit (ADR 0012) -----------------------------------------------------------------------


def limited_adapter(inbound: Any, peer: str, **options: Any) -> tuple[httpx.AsyncClient, FakeEdge]:
    edge, idp, jira, clock = FakeEdge(), IdP(), FakeJira(), Clock()
    app = create_jira_adapter_app(
        labels=LABELS,
        webhook_secret=WEBHOOK_SECRET,
        push_secret=PUSH_SECRET,
        public_base_url=PUBLIC_URL,
        edge=httpx.AsyncClient(
            transport=httpx.MockTransport(edge.handle), base_url="https://edge.example.test"
        ),
        service_token=credentials(idp, clock).token,
        jira=httpx.AsyncClient(transport=httpx.MockTransport(jira.handle), base_url=JIRA_URL),
        inbound=inbound,
        **options,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 4000)), base_url=PUBLIC_URL
    )
    return client, edge


async def test_the_webhook_is_limited_per_address_before_the_signature_check() -> None:
    inbound = Limiter(Rate(per_minute=60, burst=2), clock=Clock())
    client, edge = limited_adapter(inbound, "203.0.113.5")

    async with client:
        unsigned = [await client.post("/jira/webhook", json=issue_updated()) for _ in range(2)]
        flooded = await client.post("/jira/webhook", json=issue_updated())
        signed_but_late = await deliver(client, issue_updated())

    assert [r.status_code for r in unsigned] == [401, 401]
    assert flooded.status_code == 429 and int(flooded.headers["retry-after"]) >= 1
    assert signed_but_late.status_code == 429
    assert edge.calls == []


async def test_behind_a_trusted_proxy_the_webhook_is_limited_per_forwarded_client() -> None:
    inbound = Limiter(Rate(per_minute=60, burst=1), clock=Clock())
    client, _ = limited_adapter(inbound, "10.0.0.9", trusted_proxies=parse_networks("10.0.0.0/8"))

    async with client:
        first = {"X-Forwarded-For": "203.0.113.5"}
        await client.post("/jira/webhook", json=issue_updated(), headers=first)
        again = await client.post("/jira/webhook", json=issue_updated(), headers=first)
        other = await client.post(
            "/jira/webhook", json=issue_updated(), headers={"X-Forwarded-For": "203.0.113.6"}
        )

    assert (again.status_code, other.status_code) == (429, 401)
