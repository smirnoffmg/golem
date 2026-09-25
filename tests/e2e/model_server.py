"""A scripted OpenAI-compatible model server for the e2e suite, run in the cluster.

`POST /v1/chat/completions` in the Chat Completions wire format that `langchain_openai`'s
ChatOpenAI sends and parses (non-streaming). The request's `model` picks the mode:

- `normal`: first a tool call that fills the target's `## Evidence` through deepagents'
  `edit_file`, then, once a tool result is in the conversation, a final text message;
- `garbage`: a 200 with a JSON content type and a body that is not JSON;
- `silent`: accepts the request and never answers;
- `oversized`: the same tool call as `normal`, with megabytes of evidence.

Only the Python standard library and Starlette from the Golem image: this file is mounted
from a ConfigMap, so no fake ships in `src/`.
"""

import asyncio
import json
import re
import time
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

TARGET_PATH = re.compile(r"at `(/[^`]+\.md)`")
TARGET_RECORD = re.compile(r'<record id="[^"]+">\n(.*?)\n</record>', re.DOTALL)
EVIDENCE = "- Supports: three of five interviewed teams re-ran a decided discussion (interviews)."
OVERSIZED_BYTES = 8 * 1024 * 1024
SUMMARY = "Filled the Evidence section with one finding from interviews."


def text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return ""


def empty_evidence(system: str) -> str:
    """The target's `## Evidence` body as the brief shows it: what `edit_file` must replace."""
    record = TARGET_RECORD.search(system)
    if record is None:
        raise ValueError("no target record in the system prompt")
    _, _, after = record.group(1).partition("## Evidence\n")
    return after.split("\n## ", 1)[0].strip()


def completion(model: str, message: dict, finish_reason: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason, "logprobs": None}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


def edit_evidence(system: str, evidence: str) -> dict:
    path = TARGET_PATH.search(system)
    if path is None:
        raise ValueError("no target path in the system prompt")
    arguments = {
        "file_path": path.group(1),
        "old_string": empty_evidence(system),
        "new_string": evidence,
    }
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": "edit_file", "arguments": json.dumps(arguments)},
            }
        ],
    }


def scripted(model: str, messages: list[dict], evidence: str) -> dict:
    if any(message.get("role") == "tool" for message in messages):
        return completion(model, {"role": "assistant", "content": SUMMARY}, "stop")
    system = "\n".join(text_of(m.get("content")) for m in messages if m.get("role") == "system")
    return completion(model, edit_evidence(system, evidence), "tool_calls")


async def chat_completions(request: Request) -> Response:
    body = await request.json()
    model = body.get("model", "")
    if body.get("stream"):
        return JSONResponse({"error": {"message": "streaming is not scripted"}}, status_code=400)
    if model == "garbage":
        return Response(b"<html>upstream \xff\xfe crashed", media_type="application/json")
    if model == "silent":
        await asyncio.Event().wait()
    evidence = EVIDENCE
    if model == "oversized":
        evidence = "- " + "x" * OVERSIZED_BYTES
    return JSONResponse(scripted(model, body.get("messages", []), evidence))


async def health(_: Request) -> Response:
    return Response("ok")


app = Starlette(
    routes=[
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/healthz", health),
    ]
)
