"""The Mattermost channel adapter: ``/golem <agent> <goal>`` starts an agent, the outcome is a post.

Mattermost side: a custom slash command, whose request carries only the command's token
(``Authorization: Token <token>``, https://developers.mattermost.com/integrate/slash-commands/custom/),
and ``POST /api/v4/posts`` with a bot account's access token as Bearer.
Golem side: an A2A client of the edge, authenticated as a service with OAuth 2.0 client
credentials, receiving task updates as A2A push notifications (ADR 0010).
"""

import asyncio
import hashlib
import hmac
import logging
import re
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from golem.adapters.common import (
    NOTIFICATION_TOKEN_HEADER,
    PUSH_PATH,
    TERMINAL_STATES,
    PushUpdate,
    push_token,
    push_update,
    rate_limited,
    send_message_request,
    start_task,
    state_word,
    subject_of_push_token,
)
from golem.metrics import Instrumented, Metrics
from golem.ratelimit import COMMAND_RATE, Limiter, Network

COMMAND_PATH = "/mattermost/command"
POSTS_PATH = "/api/v4/posts"
TOKEN_SCHEME = "token"
MESSAGE_PREFIX = "mattermost:"
# Mattermost waits OutgoingIntegrationRequestsTimeout for the answer, 30 s by default, and its
# guide sends anything slower than three seconds to the response URL. Starting a task is one
# token grant and one SendMessage; past this budget the user is told it was not confirmed.
START_TIMEOUT_SECONDS = 20.0
REQUIRED_FIELDS = ("team_id", "channel_id", "user_id", "user_name", "trigger_id")
# Ids and user names go into the push token, split on ':', and the name into an @mention. Only
# the token vouches for them, so anything outside the alphabet Mattermost uses is refused.
ID = re.compile(r"[A-Za-z0-9]{1,64}")
USER_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
MENTION = re.compile(r"@(?=[A-Za-z0-9._-])")

log = logging.getLogger("golem.adapters.mattermost")


@dataclass(frozen=True)
class SlashCommand:
    team_id: str
    channel_id: str
    channel_name: str
    user_id: str
    user_name: str
    command: str
    text: str
    trigger_id: str


@dataclass(frozen=True)
class PushTarget:
    """Where a run's outcome is posted and whom it mentions; carried in the push token."""

    channel_id: str
    user_id: str
    user_name: str
    # The A2A message id: every task a replayed command creates for the same run shares it.
    run: str


def command_token_valid(expected: bytes, header: str | None) -> bool:
    scheme, _, presented = (header or "").partition(" ")
    if scheme.lower() != TOKEN_SCHEME or not presented:
        return False
    return hmac.compare_digest(presented.encode(), expected)


def slash_command(body: bytes) -> SlashCommand | None:
    try:
        form = parse_qs(body.decode(), keep_blank_values=True)
    except UnicodeDecodeError:
        return None
    values = {name: (form.get(name) or [""])[0] for name in form}
    if not all(values.get(name) for name in REQUIRED_FIELDS):
        return None
    ids = (values["team_id"], values["channel_id"], values["user_id"])
    if not all(ID.fullmatch(i) for i in ids) or not USER_NAME.fullmatch(values["user_name"]):
        return None
    return SlashCommand(
        team_id=values["team_id"],
        channel_id=values["channel_id"],
        channel_name=values.get("channel_name", ""),
        user_id=values["user_id"],
        user_name=values["user_name"],
        command=values.get("command") or "/golem",
        text=values.get("text", ""),
        trigger_id=values["trigger_id"],
    )


def agent_and_goal(text: str, agents: Collection[str]) -> tuple[str, str] | None:
    words = text.split(None, 1)
    if len(words) != 2 or words[0] not in agents:
        return None
    return words[0], words[1].strip()


def message_id(trigger_id: str) -> str:
    # Mattermost mints a trigger id per invocation (a fresh random id, the user, the time and
    # the channel, signed: GenerateTriggerId in server/public/model/integration_action.go), so
    # a replayed request carries the same one and the orchestrator starts no second run.
    return MESSAGE_PREFIX + hashlib.sha256(trigger_id.encode()).hexdigest()[:32]


def goal_text(command: SlashCommand, goal: str) -> str:
    channel = f"~{command.channel_name} ({command.channel_id})"
    return (
        f"{goal}\n\nAsked in Mattermost by @{command.user_name} (user id {command.user_id})"
        f" in {channel}."
    )


def message_metadata(command: SlashCommand) -> dict[str, Any]:
    return {
        "mattermost": {
            "userId": command.user_id,
            "userName": command.user_name,
            "teamId": command.team_id,
            "channelId": command.channel_id,
        }
    }


def push_subject(target: PushTarget) -> str:
    # User names hold letters, digits, '.', '-' and '_', never ':'; the run is split off last.
    return ":".join((target.run, target.channel_id, target.user_id, target.user_name))


def target_of_subject(subject: str) -> PushTarget | None:
    parts = subject.rsplit(":", 3)
    if len(parts) != 4 or not all(parts) or not parts[0].startswith(MESSAGE_PREFIX):
        return None
    run, channel_id, user_id, user_name = parts
    if run == MESSAGE_PREFIX:
        return None
    return PushTarget(channel_id=channel_id, user_id=user_id, user_name=user_name, run=run)


def unmentioned(text: str) -> str:
    """The text with every @-mention broken by a zero-width space: it carries the run's report,
    which an untrusted Job wrote, and must not notify @all, @channel, @here or anyone else."""
    return MENTION.sub("@\u200b", text)


def post_body(target: PushTarget, update: PushUpdate) -> dict[str, Any]:
    lines = [
        f"@{target.user_name} Golem run {state_word(update.state)}.",
        unmentioned(update.text),
        f"Task `{update.task_id}`",
    ]
    return {
        "channel_id": target.channel_id,
        "message": "\n\n".join(line for line in lines if line),
        "props": {"golem_task_id": update.task_id},
        # Each task of a run is told its outcome, and a replayed command is a second task of
        # the same run; the server returns the post already made for a repeated pending post
        # id instead of a second one (deduplicateCreatePost, server/channels/app/post.go).
        "pending_post_id": f"golem-{target.run.removeprefix(MESSAGE_PREFIX)}",
    }


def ephemeral(text: str) -> JSONResponse:
    return JSONResponse({"response_type": "ephemeral", "text": text})


def usage(command: SlashCommand, agents: Collection[str]) -> str:
    return (
        f"Usage: `{command.command} <agent> <goal>`, for example"
        f" `{command.command} {min(agents)} Redesign the checkout`."
        f" Agents: {', '.join(sorted(agents))}."
    )


def create_mattermost_adapter_app(
    *,
    command_token: bytes,
    agents: Collection[str],
    teams: Collection[str],
    channels: Collection[str],
    push_secret: bytes,
    public_base_url: str,
    edge: httpx.AsyncClient,
    service_token: Callable[[], Awaitable[str]],
    mattermost: httpx.AsyncClient,
    start_timeout_seconds: float = START_TIMEOUT_SECONDS,
    inbound: Limiter | None = None,
    trusted_proxies: tuple[Network, ...] = (),
    metrics: Metrics | None = None,
) -> Instrumented:
    if not command_token:
        raise ValueError("the slash command token must not be empty")
    push_url = f"{public_base_url}{PUSH_PATH}"
    metrics = Metrics("mattermost-adapter") if metrics is None else metrics
    inbound = Limiter(COMMAND_RATE) if inbound is None else inbound

    def allowed(command: SlashCommand) -> bool:
        return command.team_id in teams and (not channels or command.channel_id in channels)

    async def start(command: SlashCommand, agent: str, goal: str) -> str | None:
        message = message_id(command.trigger_id)
        target = PushTarget(command.channel_id, command.user_id, command.user_name, message)
        request = send_message_request(
            agent=agent,
            message=message,
            goal=goal_text(command, goal),
            push_url=push_url,
            token=push_token(push_secret, push_subject(target), message),
            metadata=message_metadata(command),
        )
        return await start_task(edge, service_token, request, what=f"{agent} for {command.user_id}")

    async def slash(request: Request) -> Response:
        refused = rate_limited(inbound, request, trusted_proxies)
        if refused is not None:
            metrics.rate_limit_refused("command")
            return refused
        if not command_token_valid(command_token, request.headers.get("Authorization")):
            metrics.authentication_failed()
            return Response(status_code=401)
        command = slash_command(await request.body())
        if command is None:
            return Response(status_code=400)
        if not allowed(command):
            return ephemeral("Golem is not enabled in this channel.")
        parsed = agent_and_goal(command.text, agents)
        if parsed is None:
            return ephemeral(usage(command, agents))
        agent, goal = parsed
        try:
            async with asyncio.timeout(start_timeout_seconds):
                task_id = await start(command, agent, goal)
        except TimeoutError:
            log.warning("starting %s for %s timed out", agent, command.user_id)
            return ephemeral(
                f"Starting {agent} was not confirmed in time. If it started, the outcome will"
                " be posted here; check before asking again, a new command starts a new run."
            )
        if task_id is None:
            return ephemeral(f"Could not start {agent}: Golem did not accept the request.")
        return ephemeral(
            f"Started {agent}, task `{task_id}`. The outcome will be posted in this channel."
        )

    async def pushed(request: Request) -> Response:
        subject = subject_of_push_token(
            push_secret, request.headers.get(NOTIFICATION_TOKEN_HEADER, "")
        )
        target = None if subject is None else target_of_subject(subject)
        if target is None:
            metrics.authentication_failed()
            return Response(status_code=401)
        try:
            update = push_update(await request.json())
        except ValueError:
            update = None
        if update is None:
            return Response(status_code=400)
        if update.state not in TERMINAL_STATES:
            return Response(status_code=204)
        try:
            response = await mattermost.post(POSTS_PATH, json=post_body(target, update))
            response.raise_for_status()
        except httpx.HTTPError as error:
            log.warning("could not post to %s for %s: %s", target.channel_id, update.task_id, error)
            return Response(status_code=502)
        return JSONResponse({"channel": target.channel_id, "task": update.task_id})

    app = Starlette(
        routes=[
            Route(COMMAND_PATH, slash, methods=["POST"]),
            Route(PUSH_PATH, pushed, methods=["POST"]),
        ]
    )
    return Instrumented(app, routes=app.routes, metrics=metrics)
