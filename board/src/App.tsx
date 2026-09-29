import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useEffect, useState } from "react";
import { type Agent, ApiError, type Session, api } from "./api";
import { AgentBoard } from "./AgentBoard";
import { Link, navigate, useRoute } from "./navigation";
import { ProposalPage } from "./ProposalPage";
import { ReportPage } from "./Reports";
import { REVIEW_COUNTS_INTERVAL_MS, ReviewPage } from "./Review";
import { REVIEW_PATH, agentPath, signinNotice } from "./route";
import { TaskPage } from "./TaskPage";

export function App() {
  const session = useQuery({
    queryKey: ["session"],
    queryFn: () => api.session(),
    staleTime: 60_000,
  });

  if (session.isPending) return <Shell />;
  if (session.error) {
    if (session.error instanceof ApiError && session.error.unauthenticated) return <SignedOut />;
    return (
      <Shell>
        <main className="notice-page">
          <p role="alert">{session.error.message}</p>
        </main>
      </Shell>
    );
  }
  return <SignedIn session={session.data} />;
}

function Shell(props: { session?: Session; children?: ReactNode }) {
  const [leaving, setLeaving] = useState<string | null>(null);

  async function signOut() {
    try {
      window.location.assign(await api.logout());
    } catch (error) {
      setLeaving(error instanceof Error ? error.message : "Signing out did not work.");
    }
  }

  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="/">
          Golem
        </a>
        {props.session && (
          <div className="whoami">
            <span className="whoami-name">{props.session.name}</span>
            <button type="button" className="quiet" onClick={signOut}>
              Sign out
            </button>
          </div>
        )}
      </header>
      {leaving && (
        <p className="banner" role="alert">
          {leaving}
        </p>
      )}
      {props.children}
    </div>
  );
}

function SignedOut() {
  const notice = signinNotice(window.location.search);
  return (
    <Shell>
      <main className="landing">
        <h1>Give Golem's agents work and review what they propose.</h1>
        <p>
          Each agent works on its own board: what is running, what waits for your answer, and
          which results are ready for you to review.
        </p>
        {notice && (
          <p className="banner" role="alert">
            {notice}
          </p>
        )}
        <a className="button" href="/login">
          Sign in
        </a>
      </main>
    </Shell>
  );
}

function SignedIn(props: { session: Session }) {
  const route = useRoute();
  const agents = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents(),
    staleTime: 60_000,
  });
  const counts = useQuery({
    queryKey: ["review-counts"],
    queryFn: () => api.reviewCounts(),
    refetchInterval: REVIEW_COUNTS_INTERVAL_MS,
  });
  const current = "agent" in route ? route.agent : null;
  const waiting = Object.values(counts.data?.agents ?? {}).reduce((sum, n) => sum + n, 0);

  return (
    <Shell session={props.session}>
      <div className="workspace">
        <nav className="rail" aria-label="Agents">
          <Link to={REVIEW_PATH} className="rail-link rail-review" current={route.page === "review"}>
            <span className="rail-name">
              To review
              {waiting > 0 && (
                <span className="count">
                  {waiting}
                  {counts.data?.more ? "+" : ""}
                </span>
              )}
            </span>
          </Link>
          <h2 className="rail-title">Agents</h2>
          {agents.isPending && <p className="muted">Loading…</p>}
          {agents.error && <p role="alert">{agents.error.message}</p>}
          {agents.data?.length === 0 && (
            <p className="muted">You may not start any agent yet. Ask your administrator.</p>
          )}
          <ul className="rail-list">
            {agents.data?.map((agent) => (
              <li key={agent.name}>
                <Link to={agentPath(agent.name)} className="rail-link" current={agent.name === current}>
                  <span className="rail-name">
                    {agent.name}
                    {(counts.data?.agents[agent.name] ?? 0) > 0 && (
                      <span className="count">{counts.data?.agents[agent.name]}</span>
                    )}
                  </span>
                  {agent.description && <span className="rail-description">{agent.description}</span>}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
        <Page route={route} agents={agents.data} />
      </div>
    </Shell>
  );
}

function Home(props: { agents: Agent[] | undefined }) {
  const only = props.agents?.length === 1 ? props.agents[0] : undefined;
  useEffect(() => {
    // One agent is the whole choice: open its board.
    if (only) navigate(agentPath(only.name));
  }, [only]);
  return (
    <main className="notice-page">
      <p className="muted">Choose an agent to see its board.</p>
    </main>
  );
}

function Page(props: { route: ReturnType<typeof useRoute>; agents: Agent[] | undefined }) {
  const { route, agents } = props;
  if (route.page === "home") return <Home agents={agents} />;
  if (route.page === "review") return <ReviewPage />;
  if (route.page === "proposal") return <ProposalPage proposalId={route.proposalId} />;
  if (route.page === "missing") {
    return (
      <main className="notice-page">
        <h1>This page does not exist.</h1>
        <p>
          <Link to="/">Go to the agents</Link>
        </p>
      </main>
    );
  }
  const agent = agents?.find((a) => a.name === route.agent);
  if (agents && !agent) {
    return (
      <main className="notice-page">
        <h1>No agent is called {route.agent}.</h1>
        <p className="muted">It is not among the agents you may start.</p>
      </main>
    );
  }
  if (route.page === "task") return <TaskPage agent={route.agent} taskId={route.taskId} />;
  if (route.page === "report") return <ReportPage agent={route.agent} taskId={route.taskId} />;
  return <AgentBoard agent={route.agent} description={agent?.description ?? ""} />;
}
