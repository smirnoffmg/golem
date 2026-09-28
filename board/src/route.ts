export type Route =
  | { page: "home" }
  | { page: "board"; agent: string }
  | { page: "task"; agent: string; taskId: string }
  | { page: "missing" };

export function agentPath(agent: string): string {
  return `/agents/${encodeURIComponent(agent)}`;
}

export function taskPath(agent: string, taskId: string): string {
  return `${agentPath(agent)}/tasks/${encodeURIComponent(taskId)}`;
}

export function parseRoute(pathname: string): Route {
  if (pathname === "/") return { page: "home" };
  const parts = pathname.split("/").slice(1);
  let decoded: string[];
  try {
    decoded = parts.map(decodeURIComponent);
  } catch {
    return { page: "missing" };
  }
  const [root, agent, tasks, taskId, ...rest] = decoded;
  if (root !== "agents" || !agent || rest.length > 0) return { page: "missing" };
  if (tasks === undefined) return { page: "board", agent };
  if (tasks === "tasks" && taskId) return { page: "task", agent, taskId };
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
