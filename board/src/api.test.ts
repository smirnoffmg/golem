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

  it("forgets the CSRF token after signing out", async () => {
    fake = fakeFetch(json(SESSION), json({ redirect: "/" }));
    const api = createApi(fake.fetch);
    await api.session();

    expect(await api.logout()).toBe("/");
    await expect(api.cancel("d", "t1")).rejects.toThrow(/session/);
  });
});
