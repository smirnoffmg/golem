import { useInfiniteQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { ProposalCard } from "./board";
import { Link } from "./navigation";
import { boardInterval } from "./poll";
import { kindName, stateText } from "./proposal";
import { proposalPath } from "./route";
import { since } from "./text";

export const REVIEW_COUNTS_INTERVAL_MS = 60_000;

// A proposal the person may decide as a reviewer: not of a task of theirs (ADR 0018).
export function ProposalCardView(props: { proposal: ProposalCard; showAgent?: boolean }) {
  const { proposal } = props;
  return (
    <article className={`card proposal-card proposal-${proposal.state}`}>
      <p className="card-kind">{kindName(proposal.kind)}</p>
      {proposal.kind === "merge_request" && proposal.url ? (
        <a className="card-goal" href={proposal.url} rel="noreferrer" target="_blank">
          {proposal.summary}
        </a>
      ) : (
        <Link to={proposalPath(proposal.id)} className="card-goal">
          {proposal.summary}
        </Link>
      )}
      <p className="card-meta">
        <span className="state">{stateText(proposal.state)}</span>
        <time dateTime={proposal.createdAt}>{since(proposal.createdAt)}</time>
        {props.showAgent && <span>{proposal.agent}</span>}
        <span>for {proposal.owner.replace(/^(user|service):/, "")}</span>
      </p>
    </article>
  );
}

export function ReviewPage() {
  const queue = useInfiniteQuery({
    queryKey: ["review-queue"],
    queryFn: ({ pageParam }) => api.reviewQueue(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next ?? undefined,
    refetchInterval: (query) => boardInterval(query.state.error),
  });
  const proposals = (queue.data?.pages ?? []).flatMap((page) => page.proposals);

  return (
    <main className="task-page review-page">
      <h1 className="board-title">To review</h1>
      <p className="muted">
        What agents propose for you to decide: as the one who started the run, or as a reviewer
        of the agent.
      </p>
      {queue.error && (
        <p className="banner" role="alert">
          {queue.error.message}
        </p>
      )}
      {queue.isPending && <p className="muted">Loading…</p>}
      {queue.data && proposals.length === 0 && <p className="empty">Nothing waits for you.</p>}
      <ul className="cards">
        {proposals.map((proposal) => (
          <li key={proposal.id}>
            <ProposalCardView proposal={proposal} showAgent />
          </li>
        ))}
      </ul>
      {queue.hasNextPage && (
        <button
          type="button"
          className="quiet"
          disabled={queue.isFetchingNextPage}
          onClick={() => void queue.fetchNextPage()}
        >
          {queue.isFetchingNextPage ? "Loading…" : "Show more"}
        </button>
      )}
    </main>
  );
}
