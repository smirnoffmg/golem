import { describe, expect, it } from "vitest";
import {
  decisionProblem,
  kindName,
  mayDecide,
  outcomeText,
  previewOf,
  stateText,
} from "./proposal";

describe("what a proposal card says", () => {
  it("names each kind a person decides on", () => {
    expect(kindName("wiki_edit")).toBe("Page edit");
    expect(kindName("desk_reply")).toBe("Service desk reply");
    expect(kindName("tracker_issue")).toBe("Tracker issue");
    expect(kindName("merge_request")).toBe("Merge request");
    expect(kindName("something_new")).toBe("something_new");
  });

  it("says where each state leaves the proposal", () => {
    expect(stateText("pending")).toBe("waiting for a decision");
    expect(stateText("failed")).toBe("could not be applied");
    expect(stateText("weird")).toBe("weird");
  });
});

describe("mayDecide", () => {
  it("lets a person decide an open proposal of a kind the platform applies", () => {
    expect(mayDecide({ kind: "wiki_edit", state: "pending" })).toBe(true);
    expect(mayDecide({ kind: "desk_reply", state: "failed" })).toBe(true);
  });

  it("leaves a merge request to GitLab and a decided proposal alone", () => {
    expect(mayDecide({ kind: "merge_request", state: "pending" })).toBe(false);
    expect(mayDecide({ kind: "wiki_edit", state: "accepted" })).toBe(false);
    expect(mayDecide({ kind: "wiki_edit", state: "applied" })).toBe(false);
  });
});

describe("decisionProblem", () => {
  it("asks a process stage's rejection for its reason", () => {
    expect(decisionProblem("reject", "  ", true)).toMatch(/why/);
    expect(decisionProblem("reject", "Wrong page.", true)).toBeNull();
  });

  it("takes a rejection without a reason outside a process, and any accept", () => {
    expect(decisionProblem("reject", "", false)).toBeNull();
    expect(decisionProblem("accept", "", true)).toBeNull();
  });

  it("keeps the reason within the limit", () => {
    expect(decisionProblem("reject", "x".repeat(4001), false)).toMatch(/4000/);
  });
});

describe("outcomeText", () => {
  it("says what came of a decision, with the target's own words when it refused", () => {
    expect(outcomeText("applied", null)).toMatch(/applied/i);
    expect(outcomeText("accepted", null)).toMatch(/being applied/);
    expect(outcomeText("stale", null)).toMatch(/changed/);
    expect(outcomeText("failed", "Jira answered 400")).toBe(
      "It could not be applied: Jira answered 400. Accept it again or reject it.",
    );
    expect(outcomeText("rejected", null)).toMatch(/rejected/i);
  });
});

describe("previewOf", () => {
  it("shows a reply's text and whether the customer sees it", () => {
    expect(previewOf("desk_reply", { request: "SD-12", public: true, text: "Fixed." })).toEqual([
      { label: "Request", value: "SD-12" },
      { label: "Seen by", value: "the customer" },
      { label: "Reply", value: "Fixed.", long: true },
    ]);
    expect(previewOf("desk_reply", { request: "SD-12", public: false, text: "x" })[1]).toEqual({
      label: "Seen by",
      value: "agents only (internal note)",
    });
  });

  it("shows a new issue's fields, or the issue and the comment", () => {
    expect(
      previewOf("tracker_issue", {
        action: "create",
        project: "OPS",
        issue_type: "Bug",
        summary: "Disk grows",
        description: "Since the 20th.",
      }),
    ).toEqual([
      { label: "Project", value: "OPS" },
      { label: "Type", value: "Bug" },
      { label: "Summary", value: "Disk grows" },
      { label: "Description", value: "Since the 20th.", long: true },
    ]);
    expect(
      previewOf("tracker_issue", { action: "comment", issue: "OPS-7", comment: "Again." }),
    ).toEqual([
      { label: "Issue", value: "OPS-7" },
      { label: "Comment", value: "Again.", long: true },
    ]);
  });

  it("shows a page edit's page and version", () => {
    expect(previewOf("wiki_edit", { page_id: "123", title: "Runbook", version: 7, body: "x" })).toEqual([
      { label: "Page", value: "Runbook (id 123)" },
      { label: "Based on version", value: "7" },
    ]);
  });

  it("shows nothing it cannot read as text", () => {
    expect(previewOf("desk_reply", { request: 3, text: { a: 1 } })).toEqual([
      { label: "Seen by", value: "agents only (internal note)" },
    ]);
  });
});
