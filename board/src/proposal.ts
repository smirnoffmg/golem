// What the board says of a proposal (ADR 0015), as pure functions of the backend's JSON. A
// payload was written in an untrusted Job: it is shown as text, field by field.

import type { Decision } from "./api";
import { MAX_REASON_CHARS } from "./process";

const KINDS: Record<string, string> = {
  wiki_edit: "Page edit",
  desk_reply: "Service desk reply",
  tracker_issue: "Tracker issue",
  merge_request: "Merge request",
};

const STATES: Record<string, string> = {
  pending: "waiting for a decision",
  accepted: "being applied",
  applied: "applied",
  rejected: "rejected",
  stale: "out of date",
  failed: "could not be applied",
};

// Open to a decision on the board; a merge request is decided in GitLab.
const DECIDABLE = new Set(["pending", "failed"]);

export type Field = { label: string; value: string; long?: boolean };

export function kindName(kind: string): string {
  return KINDS[kind] ?? kind;
}

export function stateText(state: string): string {
  return STATES[state] ?? state;
}

export function mayDecide(proposal: { kind: string; state: string }): boolean {
  return proposal.kind !== "merge_request" && DECIDABLE.has(proposal.state);
}

export function decisionProblem(decision: Decision, reason: string, stage: boolean): string | null {
  if (reason.length > MAX_REASON_CHARS) return "Keep the reason under 4000 characters.";
  // A process stage runs again with the reason (ADR 0019); elsewhere a reason is optional.
  if (decision === "reject" && stage && reason.trim() === "") {
    return "Say why you reject it: the stage runs again with your reason.";
  }
  return null;
}

export function outcomeText(state: string, detail: string | null): string {
  switch (state) {
    case "applied":
      return "Accepted and applied.";
    case "accepted":
      return "Accepted; it is being applied. The board shows when it is done.";
    case "stale":
      return "Its target changed since the agent read it, so it was not applied. The agent can redo it.";
    case "failed":
      return `It could not be applied${detail ? `: ${detail}` : ""}. Accept it again or reject it.`;
    case "rejected":
      return "Rejected.";
    default:
      return stateText(state);
  }
}

function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function fields(...entries: [string, unknown, boolean?][]): Field[] {
  const shown: Field[] = [];
  for (const [label, value, long] of entries) {
    const value_ = text(value);
    if (value_ === null) continue;
    shown.push(long ? { label, value: value_, long: true } : { label, value: value_ });
  }
  return shown;
}

export function previewOf(kind: string, payload: Record<string, unknown>): Field[] {
  switch (kind) {
    case "desk_reply":
      return fields(
        ["Request", payload.request],
        ["Seen by", payload.public === true ? "the customer" : "agents only (internal note)"],
        ["Reply", payload.text, true],
      );
    case "tracker_issue":
      if (payload.action === "comment") {
        return fields(["Issue", payload.issue], ["Comment", payload.comment, true]);
      }
      return fields(
        ["Project", payload.project],
        ["Type", payload.issue_type],
        ["Summary", payload.summary],
        ["Description", payload.description, true],
      );
    case "wiki_edit":
      return fields(
        ["Page", `${text(payload.title) ?? ""} (id ${text(payload.page_id) ?? "?"})`],
        ["Based on version", Number.isInteger(payload.version) ? String(payload.version) : null],
      );
    default:
      return [];
  }
}
