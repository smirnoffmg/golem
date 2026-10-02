import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, createApi } from "./api";

type Call = { url: string; init: RequestInit | undefined };

function fakeFetch(...answers: Response[]) {
  const calls: Call[] = [];
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const next = answers.shift();
    if (!next) throw new Error(`no answer left for ${url}`);
    return next;
  });
  return { fetch, calls };
}

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

const SESSION = { name: "alice", csrf: "token-1", expiresAt: "2026-09-28T22:00:00Z" };

describe("the API client", () => {
  let fake: ReturnType<typeof fakeFetch>;

  beforeEach(() => {
    fake = fakeFetch();
  });

  it("reads JSON with the session cookie and without following redirects", async () => {
    fake = fakeFetch(json({ agents: [] }));
    const api = createApi(fake.fetch);

    await api.agents();

    expect(fake.calls[0]?.url).toBe("/api/agents");
    expect(fake.calls[0]?.init?.credentials).toBe("same-origin");
    expect(fake.calls[0]?.init?.redirect).toBe("error");
  });

  it("sends the CSRF token it was given and a JSON body on every write", async () => {
    fake = fakeFetch(json(SESSION), json({ task: { id: "t1" } }, 201));
    const api = createApi(fake.fetch);
    await api.session();

    await api.start("discovery", "Do it", "n".repeat(32));

    const write = fake.calls[1];
    expect(write?.url).toBe("/api/agents/discovery/tasks");
    expect(write?.init?.method).toBe("POST");
    const headers = new Headers(write?.init?.headers);
    expect(headers.get("X-Golem-CSRF")).toBe("token-1");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(JSON.parse(String(write?.init?.body))).toEqual({ goal: "Do it", nonce: "n".repeat(32) });
  });

  it("refuses to write before it has a session", async () => {
    const api = createApi(fake.fetch);

    await expect(api.cancel("discovery", "t1")).rejects.toThrow(/session/);
    expect(fake.calls).toEqual([]);
  });

  it("escapes names and ids in paths", async () => {
    fake = fakeFetch(json({ id: "a/b" }));
    const api = createApi(fake.fetch);

    await api.task("x y", "a/b");

    expect(fake.calls[0]?.url).toBe("/api/agents/x%20y/tasks/a%2Fb");
  });

  it("asks for a delta with the cursor it was given", async () => {
    fake = fakeFetch(json({ agent: "d", complete: false, cursor: "c2", tasks: [] }));
    const api = createApi(fake.fetch);

    await api.board("d", "2026-09-28T10:00:00Z");

    expect(fake.calls[0]?.url).toBe("/api/agents/d/board?since=2026-09-28T10%3A00%3A00Z");
  });

  it("turns 401 into an unauthenticated error", async () => {
    fake = fakeFetch(json({ error: "unauthenticated", message: "Sign in." }, 401));
    const api = createApi(fake.fetch);

    const error = await api.agents().catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(401);
    expect((error as ApiError).unauthenticated).toBe(true);
  });

  it("keeps the code and message of a 403", async () => {
    fake = fakeFetch(json(SESSION), json({ error: "csrf", message: "Reload the page." }, 403));
    const api = createApi(fake.fetch);
    await api.session();

    const error = (await api.cancel("d", "t1").catch((e: unknown) => e)) as ApiError;

    expect(error.status).toBe(403);
    expect(error.code).toBe("csrf");
    expect(error.message).toBe("Reload the page.");
  });

  it("reads Retry-After from a 429 in seconds", async () => {
    fake = fakeFetch(
      json({ error: "rate_limited", message: "Too many." }, 429, { "Retry-After": "17" }),
    );
    const api = createApi(fake.fetch);

    const error = (await api.agents().catch((e: unknown) => e)) as ApiError;

    expect(error.status).toBe(429);
    expect(error.retryAfterSeconds).toBe(17);
  });

  it("does not trust a non-JSON error body", async () => {
    fake = fakeFetch(new Response("<html>bad gateway</html>", { status: 502 }));
    const api = createApi(fake.fetch);

    const error = (await api.agents().catch((e: unknown) => e)) as ApiError;

    expect(error.status).toBe(502);
    expect(error.code).toBe("unknown");
    expect(error.message).not.toContain("<html>");
  });

  it("reports a network failure as status 0", async () => {
    const api = createApi(async () => {
      throw new TypeError("Failed to fetch");
    });

    const error = (await api.agents().catch((e: unknown) => e)) as ApiError;

    expect(error.status).toBe(0);
  });

  it("resolves a process waiting for a reason with the action and the reason", async () => {
    fake = fakeFetch(json(SESSION), json({ taskId: "t/1", action: "rerun" }));
    const api = createApi(fake.fetch);
    await api.session();

    await api.resolve("t/1", "rerun", "Cover the API.");

    const write = fake.calls[1];
    expect(write?.url).toBe("/api/processes/t%2F1/resolution");
    expect(new Headers(write?.init?.headers).get("X-Golem-CSRF")).toBe("token-1");
    expect(JSON.parse(String(write?.init?.body))).toEqual({
      action: "rerun",
      reason: "Cover the API.",
    });
  });

  it("sends no reason to end a process", async () => {
    fake = fakeFetch(json(SESSION), json({ taskId: "t1", action: "end" }));
    const api = createApi(fake.fetch);
    await api.session();

    await api.resolve("t1", "end");

    expect(JSON.parse(String(fake.calls[1]?.init?.body))).toEqual({ action: "end" });
  });

  it("keeps the code of a process that no longer waits", async () => {
    fake = fakeFetch(
      json(SESSION),
      json({ error: "not_waiting", message: "The process no longer waits." }, 409),
    );
    const api = createApi(fake.fetch);
    await api.session();

    const error = (await api.resolve("t1", "end").catch((e: unknown) => e)) as ApiError;

    expect(error.code).toBe("not_waiting");
    expect(error.status).toBe(409);
  });

  it("decides a proposal with the decision and a trimmed reason", async () => {
    fake = fakeFetch(json(SESSION), json({ id: "p/1", state: "rejected" }));
    const api = createApi(fake.fetch);
    await api.session();

    const decided = await api.decide("p/1", "reject", "  Too curt.  ");

    expect(decided.state).toBe("rejected");
    expect(fake.calls[1]?.url).toBe("/api/proposals/p%2F1/decision");
    expect(JSON.parse(String(fake.calls[1]?.init?.body))).toEqual({
      decision: "reject",
      reason: "Too curt.",
    });
  });

  it("sends no reason to accept", async () => {
    fake = fakeFetch(json(SESSION), json({ id: "p1", state: "applied" }));
    const api = createApi(fake.fetch);
    await api.session();

    await api.decide("p1", "accept", "");

    expect(JSON.parse(String(fake.calls[1]?.init?.body))).toEqual({ decision: "accept" });
  });

  it("keeps the code of a proposal someone decided first", async () => {
    fake = fakeFetch(
      json(SESSION),
      json({ error: "already_decided", message: "Someone decided on this proposal already." }, 409),
    );
    const api = createApi(fake.fetch);
    await api.session();

    const error = (await api.decide("p1", "accept", "").catch((e: unknown) => e)) as ApiError;

    expect(error.code).toBe("already_decided");
    expect(error.status).toBe(409);
  });

  it("reads the review queue, an agent's reports and one report by page", async () => {
    fake = fakeFetch(
      json({ proposals: [], next: null }),
      json({ reports: [], next: null }),
      json({ taskId: "t 1", agent: "d", target: null, completedAt: "x", text: "y" }),
    );
    const api = createApi(fake.fetch);

    await api.reviewQueue("tok=");
    await api.reports("a b", "p2");
    await api.report("t 1");

    expect(fake.calls.map((c) => c.url)).toEqual([
      "/api/proposals?page=tok%3D",
      "/api/agents/a%20b/reports?page=p2",
      "/api/reports/t%201",
    ]);
  });

  it("forgets the CSRF token after signing out", async () => {
    fake = fakeFetch(json(SESSION), json({ redirect: "/" }));
    const api = createApi(fake.fetch);
    await api.session();

    expect(await api.logout()).toBe("/");
    await expect(api.cancel("d", "t1")).rejects.toThrow(/session/);
  });
});
