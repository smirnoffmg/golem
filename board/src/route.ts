export type Route =
  | { page: "home" }
  | { page: "board"; agent: string }
  | { page: "task"; agent: string; taskId: string }
  | { page: "report"; agent: string; taskId: string }
  | { page: "review" }
  | { page: "proposal"; proposalId: string }
  | { page: "missing" };

export function agentPath(agent: string): string {
  return `/agents/${encodeURIComponent(agent)}`;
}

export function taskPath(agent: string, taskId: string): string {
  return `${agentPath(agent)}/tasks/${encodeURIComponent(taskId)}`;
}

export function reportPath(agent: string, taskId: string): string {
  return `${agentPath(agent)}/reports/${encodeURIComponent(taskId)}`;
}

export function proposalPath(proposalId: string): string {
  return `/proposals/${encodeURIComponent(proposalId)}`;
}

export const REVIEW_PATH = "/review";

export function parseRoute(pathname: string): Route {
  if (pathname === "/") return { page: "home" };
  if (pathname === REVIEW_PATH) return { page: "review" };
  const parts = pathname.split("/").slice(1);
  let decoded: string[];
  try {
    decoded = parts.map(decodeURIComponent);
  } catch {
    return { page: "missing" };
  }
  const [root, agent, kind, taskId, ...rest] = decoded;
  if (root === "proposals" && agent && kind === undefined) {
    return { page: "proposal", proposalId: agent };
  }
  if (root !== "agents" || !agent || rest.length > 0) return { page: "missing" };
  if (kind === undefined) return { page: "board", agent };
  if (kind === "tasks" && taskId) return { page: "task", agent, taskId };
  if (kind === "reports" && taskId) return { page: "report", agent, taskId };
  return { page: "missing" };
}

const SIGNIN_NOTICES: Record<string, string> = {
  expired: "The sign-in took too long or was started in another browser. Sign in again.",
  refused: "The identity provider did not sign you in. Sign in again, or ask your administrator.",
  failed: "Signing in did not work. Try again in a moment.",
};

export function signinNotice(search: string): string | null {
  const code = new URLSearchParams(search).get("signin");
  return (code !== null && SIGNIN_NOTICES[code]) || null;
}
