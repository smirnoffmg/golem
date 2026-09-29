// The board's only door to Golem: the backend-for-frontend on the same origin (ADR 0018). The
// browser never holds a token; the session cookie rides along, and the CSRF token the session
// hands out lives in this closure only.

import type { BoardResponse, ProposalCard, Task } from "./board";

export type Session = { name: string; csrf: string; expiresAt: string };
export type Agent = { name: string; description: string; skills: string[] };
export type TaskDetail = Task & {
  history: { role: "user" | "agent"; text: string }[];
  artifacts: { name: string; text: string }[];
};
export type TaskPage = { tasks: Task[]; next: string | null };
export type Resolution = "rerun" | "end";
export type Decision = "accept" | "reject";
// A page edit's diff as the backend folds it: unchanged runs far from a change are counted.
export type DiffRun =
  | { op: "equal" | "insert" | "delete"; lines: string[] }
  | { op: "fold"; count: number };
export type ProposalDetail = ProposalCard & {
  payload: Record<string, unknown>;
  target: string | null;
  reason: string | null;
  detail: string | null;
  report: string | null;
  stage: boolean;
  live: { title: unknown; version: unknown } | null;
  liveError: string | null;
  diff: DiffRun[] | null;
};
export type ProposalQueue = { proposals: ProposalCard[]; next: string | null };
export type ReviewCounts = { agents: Record<string, number>; more: boolean };
export type ReportCard = {
  taskId: string;
  agent: string;
  target: string | null;
  completedAt: string;
  summary: string;
};
export type ReportPage = { reports: ReportCard[]; next: string | null };
export type ReportDetail = Omit<ReportCard, "summary"> & { text: string };

type Fetch = (url: string, init?: RequestInit) => Promise<Response>;

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryAfterSeconds: number | null;

  constructor(status: number, code: string, message: string, retryAfterSeconds: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryAfterSeconds = retryAfterSeconds;
  }

  get unauthenticated(): boolean {
    return this.status === 401;
  }
}

const FALLBACK_MESSAGES: Record<number, string> = {
  0: "Golem cannot be reached. Check your connection.",
  502: "Golem did not answer. Try again in a moment.",
};

async function errorOf(response: Response): Promise<ApiError> {
  const retryAfter = Number.parseInt(response.headers.get("Retry-After") ?? "", 10);
  let code = "unknown";
  let message = FALLBACK_MESSAGES[response.status] ?? `The request failed (${response.status}).`;
  if (response.headers.get("Content-Type")?.includes("application/json")) {
    const body: unknown = await response.json().catch(() => null);
    if (body && typeof body === "object") {
      const fields = body as { error?: unknown; message?: unknown };
      if (typeof fields.error === "string") code = fields.error;
      if (typeof fields.message === "string" && fields.message) message = fields.message;
    }
  }
  return new ApiError(
    response.status,
    code,
    message,
    Number.isFinite(retryAfter) && retryAfter >= 0 ? retryAfter : null,
  );
}

function segment(value: string): string {
  return encodeURIComponent(value);
}

export function createApi(fetch: Fetch) {
  let csrf: string | null = null;

  async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
    let response: Response;
    try {
      // A fetch must never follow a redirect to the identity provider: the backend answers
      // 401 instead, and the board navigates itself.
      response = await fetch(url, { credentials: "same-origin", redirect: "error", ...init });
    } catch {
      throw new ApiError(0, "network", FALLBACK_MESSAGES[0] ?? "");
    }
    if (!response.ok) throw await errorOf(response);
    return (await response.json()) as T;
  }

  function write<T>(url: string, body: object): Promise<T> {
    if (csrf === null) {
      return Promise.reject(new ApiError(401, "unauthenticated", "There is no session to act in."));
    }
    return request<T>(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Golem-CSRF": csrf },
      body: JSON.stringify(body),
    });
  }

  const tasksOf = (agent: string) => `/api/agents/${segment(agent)}/tasks`;
  const paged = (page?: string) => (page === undefined ? "" : `?page=${encodeURIComponent(page)}`);

  return {
    async session(): Promise<Session> {
      const session = await request<Session>("/api/session");
      csrf = session.csrf;
      return session;
    },
    async agents(): Promise<Agent[]> {
      return (await request<{ agents: Agent[] }>("/api/agents")).agents;
    },
    board(agent: string, since?: string): Promise<BoardResponse> {
      const query = since === undefined ? "" : `?since=${encodeURIComponent(since)}`;
      return request<BoardResponse>(`/api/agents/${segment(agent)}/board${query}`);
    },
    olderTasks(agent: string, page?: string): Promise<TaskPage> {
      const query = page === undefined ? "" : `?page=${encodeURIComponent(page)}`;
      return request<TaskPage>(`${tasksOf(agent)}${query}`);
    },
    task(agent: string, id: string): Promise<TaskDetail> {
      return request<TaskDetail>(`${tasksOf(agent)}/${segment(id)}`);
    },
    async start(agent: string, goal: string, nonce: string): Promise<Task> {
      return (await write<{ task: Task }>(tasksOf(agent), { goal, nonce })).task;
    },
    async reply(agent: string, id: string, text: string, nonce: string): Promise<Task> {
      const url = `${tasksOf(agent)}/${segment(id)}/messages`;
      return (await write<{ task: Task }>(url, { text, nonce })).task;
    },
    async cancel(agent: string, id: string): Promise<Task> {
      return (await write<{ task: Task }>(`${tasksOf(agent)}/${segment(id)}/cancel`, {})).task;
    },
    async resolve(taskId: string, action: Resolution, reason?: string): Promise<void> {
      const url = `/api/processes/${segment(taskId)}/resolution`;
      await write(url, reason === undefined ? { action } : { action, reason });
    },
    reviewCounts(): Promise<ReviewCounts> {
      return request<ReviewCounts>("/api/review-counts");
    },
    reviewQueue(page?: string): Promise<ProposalQueue> {
      return request<ProposalQueue>(`/api/proposals${paged(page)}`);
    },
    proposal(id: string): Promise<ProposalDetail> {
      return request<ProposalDetail>(`/api/proposals/${segment(id)}`);
    },
    decide(id: string, decision: Decision, reason: string): Promise<ProposalDetail> {
      const trimmed = reason.trim();
      const body = trimmed === "" ? { decision } : { decision, reason: trimmed };
      return write<ProposalDetail>(`/api/proposals/${segment(id)}/decision`, body);
    },
    reports(agent: string, page?: string): Promise<ReportPage> {
      return request<ReportPage>(`/api/agents/${segment(agent)}/reports${paged(page)}`);
    },
    report(taskId: string): Promise<ReportDetail> {
      return request<ReportDetail>(`/api/reports/${segment(taskId)}`);
    },
    async logout(): Promise<string> {
      const { redirect } = await write<{ redirect: string }>("/logout", {});
      csrf = null;
      return redirect;
    },
  };
}

export type Api = ReturnType<typeof createApi>;

export const api = createApi((url, init) => window.fetch(url, init));
