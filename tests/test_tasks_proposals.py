"""The task service's proposal and report routes (ADR 0015, ADR 0018): what it answers the edge
for a person, how a decision is applied through the write servers, and how an accepted proposal
whose apply was lost is applied again when the reconciler asks."""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send
from test_tasks_service import TEST_EDGE_TOKEN, make_card
from test_tasks_to_runs import CATALOG, GRANTS, SIGNING_KEY, TEMPLATE, FakeLauncher

from golem.decisions import REVIEWS_HEADER
from golem.metrics import Metrics
from golem.orchestrator.admission import Limits
from golem.orchestrator.service import PostgresOrchestrator
from golem.proposal_payload import payload_digest
from golem.tasks.app import (
    EDGE_TOKEN_HEADER,
    PRINCIPAL_HEADER,
    PROPOSAL_STATE_PATH,
    create_listeners,
)
from golem.tasks.apply import ApplyUnavailable
from golem.tasks.ports import Applied, LivePage, ProposalDetail

REPLY = {"request": "SD-12", "public": True, "text": "The export works again."}
PAGE = {"page_id": "123", "title": "Home", "version": 7, "body": "<p>New</p>"}


@dataclass
class FakeApplier:
    result: Applied | Exception = field(default_factory=lambda: Applied("applied"))
    live: LivePage | Exception = field(
        default_factory=lambda: LivePage(title="Home", version=7, body="<p>Old</p>")
    )
    applied: list[ProposalDetail] = field(default_factory=list)
    previews: list[tuple[str, str]] = field(default_factory=list)

    async def apply(self, decided: ProposalDetail) -> Applied:
        self.applied.append(decided)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def preview(self, proposal: ProposalDetail, reader: str) -> LivePage:
        self.previews.append((proposal.summary.id, reader))
        if isinstance(self.live, Exception):
            raise self.live
        return self.live


@pytest.fixture
def applier() -> FakeApplier:
    return FakeApplier()


def _orchestrator(runs_db: str) -> PostgresOrchestrator:
    return PostgresOrchestrator(
        dsn=runs_db,
        limits=Limits(max_runs_per_caller=10, max_runs_per_root=10, budget_per_root=Decimal("99")),
        estimated_cost=Decimal("1"),
        launcher=FakeLauncher(),
        template=TEMPLATE,
        catalogs={"desk": CATALOG, "wiki": CATALOG},
        signing_key=SIGNING_KEY,
        grants=GRANTS,
    )


@pytest.fixture
def client(runs_db: str, applier: FakeApplier) -> Iterator[TestClient]:
    listeners = create_listeners(
        make_card(), _orchestrator(runs_db), edge_token=TEST_EDGE_TOKEN, applier=applier
    )

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == PROPOSAL_STATE_PATH:
            await listeners.internal_write(scope, receive, send)
            return
        if scope["type"] == "http" and scope["path"].startswith("/internal/"):
            await listeners.internal_read(scope, receive, send)
            return
        token = (EDGE_TOKEN_HEADER.encode(), TEST_EDGE_TOKEN.encode())
        public: ASGIApp = listeners.public
        await public({**scope, "headers": [*scope.get("headers", []), token]}, receive, send)

    with TestClient(app) as test_client:
        yield test_client


def as_person(principal: str, reviews: str = "") -> dict[str, str]:
    return {PRINCIPAL_HEADER: principal, REVIEWS_HEADER: reviews}


def start(client: TestClient, agent: str, principal: str = "user:alice") -> str:
    body = client.post(
        "/a2a",
        headers={"A2A-Version": "1.0", **as_person(principal)},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "tenant": agent,
                "message": {
                    "role": "ROLE_USER",
                    "messageId": uuid.uuid4().hex,
                    "parts": [{"text": "go"}],
                },
            },
        },
    ).json()
    return body["result"]["task"]["id"]


def proposal_for(
    dsn: str, task_id: str, kind: str = "desk_reply", payload: dict[str, Any] = REPLY
) -> str:
    proposal_id = str(uuid.uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE runs SET status = 'succeeded', proposal_settled_at = now()"
            " FROM run_tasks t WHERE t.run_id = runs.id AND t.task_id = %s",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO proposals (id, run_id, task_id, agent, owner, kind, state,"
            " notified_state, payload, digest, commit, target)"
            " SELECT %s, r.id, r.task_id, r.agent, r.caller, %s, 'pending', 'pending', %s, %s,"
            " 'sha-1', 'alert-1' FROM run_tasks t JOIN runs r ON r.id = t.run_id"
            " WHERE t.task_id = %s",
            (proposal_id, kind, Jsonb(payload), payload_digest(payload), task_id),
        )
    return proposal_id


def state_of(dsn: str, proposal_id: str) -> tuple[str, str | None]:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT state, detail FROM proposals WHERE id = %s", (proposal_id,)
        ).fetchone()
    assert row is not None
    return row


def test_a_person_lists_their_proposals_and_their_reviews(client: TestClient, runs_db: str) -> None:
    own = proposal_for(runs_db, start(client, "desk"))
    theirs = proposal_for(runs_db, start(client, "desk", principal="service:jira"))

    alice = client.get("/proposals", headers=as_person("user:alice")).json()
    bob = client.get("/proposals?agent=desk", headers=as_person("user:bob", "desk,wiki")).json()

    assert [p["id"] for p in alice["proposals"]] == [own]
    assert [p["id"] for p in bob["proposals"]] == [theirs, own]
    item = bob["proposals"][0]
    assert {k: item[k] for k in ("agent", "kind", "state", "summary", "owner")} == {
        "agent": "desk",
        "kind": "desk_reply",
        "state": "pending",
        "summary": "Reply to SD-12",
        "owner": "service:jira",
    }
    assert bob["next"] is None


@pytest.mark.parametrize("query", ["state=unknown", "agent=Bad!", "page=%%%", "process=Nope Nope"])
def test_a_malformed_list_query_is_refused(client: TestClient, query: str) -> None:
    response = client.get(f"/proposals?{query}", headers=as_person("user:alice"))

    assert response.status_code == 400
    assert response.json() == {"error": "malformed"}


def test_one_proposal_reads_with_its_payload(client: TestClient, runs_db: str) -> None:
    proposal_id = proposal_for(runs_db, start(client, "desk"))

    found = client.get(f"/proposals/{proposal_id}", headers=as_person("user:alice"))
    hidden = client.get(f"/proposals/{proposal_id}", headers=as_person("user:carol", "wiki"))

    assert found.status_code == 200
    assert found.json()["payload"] == REPLY
    assert found.json()["target"] == "alert-1"
    assert hidden.status_code == 404


def test_a_wiki_edit_reads_with_the_live_page_to_diff_against(
    client: TestClient, runs_db: str, applier: FakeApplier
) -> None:
    proposal_id = proposal_for(runs_db, start(client, "wiki"), "wiki_edit", PAGE)

    body = client.get(f"/proposals/{proposal_id}", headers=as_person("user:alice")).json()

    assert body["live"] == {"title": "Home", "version": 7, "body": "<p>Old</p>"}
    assert body["state"] == "pending"
    assert applier.previews == [(proposal_id, "user:alice")]


def test_a_page_changed_since_the_role_read_it_makes_the_proposal_stale(
    client: TestClient, runs_db: str, applier: FakeApplier
) -> None:
    proposal_id = proposal_for(runs_db, start(client, "wiki"), "wiki_edit", PAGE)
    applier.live = LivePage(title="Home", version=8, body="<p>Someone's edit</p>")

    body = client.get(f"/proposals/{proposal_id}", headers=as_person("user:alice")).json()

    assert body["state"] == "stale"
    assert state_of(runs_db, proposal_id)[0] == "stale"


def test_a_live_page_that_cannot_be_read_still_shows_the_proposal(
    client: TestClient, runs_db: str, applier: FakeApplier
) -> None:
    proposal_id = proposal_for(runs_db, start(client, "wiki"), "wiki_edit", PAGE)
    applier.live = ApplyUnavailable("Confluence is down")

    body = client.get(f"/proposals/{proposal_id}", headers=as_person("user:alice")).json()

    assert body["live"] is None
    assert "Confluence is down" in body["liveError"]
    assert body["state"] == "pending"


def decide(
    client: TestClient, proposal_id: str, body: dict[str, Any], principal: str = "user:alice"
) -> Any:
    return client.post(
        f"/proposals/{proposal_id}/decision", json=body, headers=as_person(principal)
    )


def test_accepting_applies_at_once_and_answers_with_the_result(
    client: TestClient, runs_db: str, applier: FakeApplier
) -> None:
    task_id = start(client, "desk")
    proposal_id = proposal_for(runs_db, task_id)

    response = decide(client, proposal_id, {"decision": "accept"})

    assert response.status_code == 200
    assert response.json()["state"] == "applied"
    [applied] = applier.applied
    assert (applied.summary.state, applied.summary.decided_by) == ("accepted", "user:alice")
    assert state_of(runs_db, proposal_id) == ("applied", None)


def test_the_task_shows_the_decided_state(client: TestClient, runs_db: str) -> None:
    task_id = start(client, "desk")
    proposal_id = proposal_for(runs_db, task_id)

    decide(client, proposal_id, {"decision": "accept"})

    task = client.post(
        "/a2a",
        headers={"A2A-Version": "1.0", **as_person("user:alice")},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "GetTask",
            "params": {"tenant": "desk", "id": task_id},
        },
    ).json()["result"]
    assert task["metadata"]["golemProposal"]["state"] == "applied"


def test_an_upstream_refusal_is_a_failed_proposal(
    client: TestClient, runs_db: str, applier: FakeApplier
) -> None:
    proposal_id = proposal_for(runs_db, start(client, "desk"))
    applier.result = Applied("failed", "Jira answered 403")

    body = decide(client, proposal_id, {"decision": "accept"}).json()

    assert (body["state"], body["detail"]) == ("failed", "Jira answered 403")


def test_an_apply_that_does_not_answer_leaves_the_proposal_accepted(
    client: TestClient, runs_db: str, applier: FakeApplier
) -> None:
    proposal_id = proposal_for(runs_db, start(client, "desk"))
    applier.result = ApplyUnavailable("write server unreachable")

    body = decide(client, proposal_id, {"decision": "accept"}).json()

    assert body["state"] == "accepted"
    assert state_of(runs_db, proposal_id) == ("accepted", None)


def expire_lease(dsn: str, proposal_id: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE proposals SET apply_lease_until = now() - interval '1 second' WHERE id = %s",
            (proposal_id,),
        )


def test_a_notification_while_the_decisions_apply_holds_it_applies_nothing_more(
    client: TestClient, runs_db: str, applier: FakeApplier
) -> None:
    proposal_id = proposal_for(runs_db, start(client, "desk"))
    # The decision's apply outlived its 15 s answer and may still be writing.
    applier.result = ApplyUnavailable("write server did not answer")
    decide(client, proposal_id, {"decision": "accept"})
    applier.result = Applied("applied")

    # The reconciler delivers the accepted state, and a retry comes early: neither applies
    # while the first apply's lease holds, or a reply would be posted twice.
    response = client.post(PROPOSAL_STATE_PATH, json={"proposal_id": proposal_id})

    assert response.status_code == 200
    assert len(applier.applied) == 1
    assert state_of(runs_db, proposal_id) == ("accepted", None)


def test_the_reconciler_asking_again_applies_an_accepted_proposal(
    client: TestClient, runs_db: str, applier: FakeApplier
) -> None:
    proposal_id = proposal_for(runs_db, start(client, "desk"))
    applier.result = ApplyUnavailable("write server unreachable")
    decide(client, proposal_id, {"decision": "accept"})
    applier.result = Applied("applied")
    expire_lease(runs_db, proposal_id)

    response = client.post(PROPOSAL_STATE_PATH, json={"proposal_id": proposal_id})

    assert response.status_code == 200
    assert state_of(runs_db, proposal_id) == ("applied", None)
    assert len(applier.applied) == 2


def test_rejecting_applies_nothing(client: TestClient, runs_db: str, applier: FakeApplier) -> None:
    proposal_id = proposal_for(runs_db, start(client, "desk"))

    body = decide(client, proposal_id, {"decision": "reject", "reason": "Wrong customer."}).json()

    assert (body["state"], body["reason"]) == ("rejected", "Wrong customer.")
    assert applier.applied == []


@pytest.mark.parametrize(
    ("setup", "body", "status", "error"),
    [
        ("none", {"decision": "accept"}, 409, "already_decided"),
        ("mr", {"decision": "accept"}, 409, "decided_in_gitlab"),
        ("none", {"decision": "maybe"}, 400, "malformed"),
        ("none", {"decision": "reject", "reason": 7}, 400, "malformed"),
        ("none", {"decision": "reject", "reason": "x" * 4001}, 400, "malformed"),
    ],
)
def test_a_decision_is_refused_with_its_reason(
    client: TestClient, runs_db: str, setup: str, body: dict, status: int, error: str
) -> None:
    kind, payload = ("merge_request", {"iid": 1}) if setup == "mr" else ("desk_reply", REPLY)
    proposal_id = proposal_for(runs_db, start(client, "desk"), kind, payload)
    if error == "already_decided":
        decide(client, proposal_id, {"decision": "reject", "reason": "no"})

    response = decide(client, proposal_id, body)

    assert (response.status_code, response.json()) == (status, {"error": error})


def test_a_person_no_proposal_token_can_name_is_refused_before_anything_moves(
    client: TestClient, runs_db: str, applier: FakeApplier
) -> None:
    proposal_id = proposal_for(runs_db, start(client, "desk", principal="user:john doe"))

    response = decide(client, proposal_id, {"decision": "accept"}, principal="user:john doe")

    assert (response.status_code, response.json()) == (403, {"error": "unnameable_decider"})
    assert state_of(runs_db, proposal_id) == ("pending", None)
    assert applier.applied == []


def test_nobody_else_decides(client: TestClient, runs_db: str) -> None:
    proposal_id = proposal_for(runs_db, start(client, "desk"))

    response = decide(client, proposal_id, {"decision": "accept"}, principal="user:carol")

    assert response.status_code == 404


def reported(dsn: str, task_id: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE runs SET status = 'succeeded', proposal_settled_at = now(),"
            " outcome = 'reported', record = 'reports/alert-1.md', report = %s"
            " FROM run_tasks t WHERE t.run_id = runs.id AND t.task_id = %s",
            ("Disk grows 4% a day.\n\nNothing to act on yet.", task_id),
        )


def test_a_reviewer_reads_the_reports_of_an_agent_they_review(
    client: TestClient, runs_db: str
) -> None:
    task_id = start(client, "desk", principal="service:alertmanager")
    reported(runs_db, task_id)
    reviewer = as_person("user:bob", "desk")

    listed = client.get("/reports?agent=desk", headers=reviewer).json()
    one = client.get(f"/reports/{task_id}", headers=reviewer)
    hidden = client.get(f"/reports/{task_id}", headers=as_person("user:alice"))

    [item] = listed["reports"]
    assert {k: item[k] for k in ("taskId", "agent", "target", "summary")} == {
        "taskId": task_id,
        "agent": "desk",
        "target": "alert-1",
        "summary": "Disk grows 4% a day.",
    }
    assert one.json()["text"] == "Disk grows 4% a day.\n\nNothing to act on yet."
    assert hidden.status_code == 404


def test_a_write_server_reads_a_proposals_state_digest_and_kind_and_nothing_else(
    client: TestClient, runs_db: str
) -> None:
    task_id = start(client, "desk")
    proposal_id = proposal_for(runs_db, task_id)
    decide(client, proposal_id, {"decision": "reject", "reason": "Wrong customer."})

    found = client.get(f"/internal/proposals/{proposal_id}")
    unknown = client.get(f"/internal/proposals/{uuid.uuid4()}")
    malformed = client.get("/internal/proposals/not-a-uuid")

    assert found.status_code == 200
    assert found.json() == {
        "id": proposal_id,
        "state": "rejected",
        "digest": payload_digest(REPLY),
        "kind": "desk_reply",
    }
    assert (unknown.status_code, malformed.status_code) == (404, 404)


def test_the_proposal_route_is_not_on_the_edges_port(client: TestClient, runs_db: str) -> None:
    task_id = start(client, "desk")
    proposal_id = proposal_for(runs_db, task_id)
    listeners = create_listeners(make_card(), _orchestrator(runs_db), edge_token=TEST_EDGE_TOKEN)
    token = {EDGE_TOKEN_HEADER: TEST_EDGE_TOKEN}

    with TestClient(listeners.public) as public, TestClient(listeners.internal_write) as write:
        assert public.get(f"/internal/proposals/{proposal_id}", headers=token).status_code == 404
        assert write.get(f"/internal/proposals/{proposal_id}").status_code == 404


def test_decisions_and_applies_are_counted_by_kind_and_result(runs_db: str) -> None:
    metrics = Metrics("tasks")
    applier = FakeApplier(result=Applied("stale", "Page 123 is at version 9."))
    listeners = create_listeners(
        make_card(),
        _orchestrator(runs_db),
        edge_token=TEST_EDGE_TOKEN,
        applier=applier,
        metrics=metrics,
    )

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        token = (EDGE_TOKEN_HEADER.encode(), TEST_EDGE_TOKEN.encode())
        await listeners.public(
            {**scope, "headers": [*scope.get("headers", []), token]}, receive, send
        )

    with TestClient(app) as client:
        accepted = proposal_for(runs_db, start(client, "wiki"), "wiki_edit", PAGE)
        rejected = proposal_for(runs_db, start(client, "desk"))
        decide(client, accepted, {"decision": "accept"})
        decide(client, rejected, {"decision": "reject"})
        applier.result = ApplyUnavailable("no answer")
        again = proposal_for(runs_db, start(client, "desk"))
        decide(client, again, {"decision": "accept"})

    def decided(kind: str, decision: str) -> float | None:
        labels = {"kind": kind, "decision": decision}
        return metrics.registry.get_sample_value("golem_proposal_decisions_total", labels)

    def applied(kind: str, result: str) -> float | None:
        labels = {"kind": kind, "result": result}
        return metrics.registry.get_sample_value("golem_proposal_applies_total", labels)

    assert decided("wiki_edit", "accept") == 1
    assert decided("desk_reply", "reject") == 1
    assert decided("desk_reply", "accept") == 1
    assert applied("wiki_edit", "stale") == 1
    assert applied("desk_reply", "unanswered") == 1


def test_an_apply_whose_result_finds_the_row_moved_on_is_logged_and_counted(
    runs_db: str, caplog: pytest.LogCaptureFixture
) -> None:
    metrics = Metrics("tasks")
    applier = FakeApplier()
    listeners = create_listeners(
        make_card(),
        _orchestrator(runs_db),
        edge_token=TEST_EDGE_TOKEN,
        applier=applier,
        metrics=metrics,
    )

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        token = (EDGE_TOKEN_HEADER.encode(), TEST_EDGE_TOKEN.encode())
        await listeners.public(
            {**scope, "headers": [*scope.get("headers", []), token]}, receive, send
        )

    with TestClient(app) as client:
        proposal_id = proposal_for(runs_db, start(client, "desk"))
        original = applier.apply

        async def moved_meanwhile(decided: ProposalDetail) -> Applied:
            # Something else moved the row while the write server wrote.
            with psycopg.connect(runs_db, autocommit=True) as conn:
                conn.execute(
                    "UPDATE proposals SET state = 'rejected' WHERE id = %s", (proposal_id,)
                )
            return await original(decided)

        applier.apply = moved_meanwhile  # type: ignore[method-assign]
        decide(client, proposal_id, {"decision": "accept"})

    labels = {"kind": "desk_reply", "result": "unrecorded"}
    assert metrics.registry.get_sample_value("golem_proposal_applies_total", labels) == 1
    assert f"proposal {proposal_id} was applied" in caplog.text
