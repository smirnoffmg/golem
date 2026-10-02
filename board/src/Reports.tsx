import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api";
import { Link } from "./navigation";
import { agentPath, reportPath } from "./route";
import { since } from "./text";

// Open, the lane refetches its first page every minute; closed, it costs nothing (ADR 0018).
export const REPORTS_INTERVAL_MS = 60_000;

export function ReportsLane(props: { agent: string }) {
  const [open, setOpen] = useState(false);
  const reports = useInfiniteQuery({
    queryKey: ["reports", props.agent],
    queryFn: ({ pageParam }) => api.reports(props.agent, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next ?? undefined,
    enabled: open,
    refetchInterval: open ? REPORTS_INTERVAL_MS : false,
  });
  const items = (reports.data?.pages ?? []).flatMap((page) => page.reports);

  return (
    <details
      className="reports"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>Reports</summary>
      <p className="muted">What the agent's runs found without proposing anything.</p>
      {reports.isFetching && !reports.data && <p className="muted">Loading…</p>}
      {reports.error && <p role="alert">{reports.error.message}</p>}
      {reports.data && items.length === 0 && <p className="empty">No report yet.</p>}
      <ul className="cards archive-cards">
        {items.map((report) => (
          <li key={report.taskId}>
            <article className="card">
              <Link to={reportPath(props.agent, report.taskId)} className="card-goal">
                {report.summary || "(empty report)"}
              </Link>
              <p className="card-meta">
                <time dateTime={report.completedAt}>{since(report.completedAt)}</time>
                {report.target && <span>{report.target}</span>}
              </p>
            </article>
          </li>
        ))}
      </ul>
      {reports.hasNextPage && (
        <button
          type="button"
          className="quiet"
          disabled={reports.isFetchingNextPage}
          onClick={() => void reports.fetchNextPage()}
        >
          {reports.isFetchingNextPage ? "Loading…" : "Show older reports"}
        </button>
      )}
    </details>
  );
}

export function ReportPage(props: { agent: string; taskId: string }) {
  const report = useQuery({
    queryKey: ["report", props.taskId],
    queryFn: () => api.report(props.taskId),
  });
  return (
    <main className="task-page">
      <p>
        <Link to={agentPath(props.agent)}>Back to the {props.agent} board</Link>
      </p>
      {report.isPending && <p className="muted">Loading the report…</p>}
      {report.error && (
        <p className="banner" role="alert">
          {report.error.message}
        </p>
      )}
      {report.data && (
        <>
          <h1 className="task-goal">Report of {report.data.agent}</h1>
          <dl className="facts">
            <dt>Finished</dt>
            <dd>
              <time dateTime={report.data.completedAt}>{since(report.data.completedAt)}</time>
            </dd>
            {report.data.target && (
              <>
                <dt>Target</dt>
                <dd>{report.data.target}</dd>
              </>
            )}
          </dl>
          <pre className="report-text">{report.data.text}</pre>
        </>
      )}
    </main>
  );
}
