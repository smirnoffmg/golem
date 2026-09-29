import { describe, expect, it } from "vitest";
import { agentPath, parseRoute, proposalPath, reportPath, signinNotice, taskPath } from "./route";

describe("parseRoute", () => {
  it("knows the three pages", () => {
    expect(parseRoute("/")).toEqual({ page: "home" });
    expect(parseRoute("/agents/discovery")).toEqual({ page: "board", agent: "discovery" });
    expect(parseRoute("/agents/discovery/tasks/t-1")).toEqual({
      page: "task",
      agent: "discovery",
      taskId: "t-1",
    });
  });

  it("decodes what it built", () => {
    expect(parseRoute(taskPath("a b", "x/y"))).toEqual({ page: "task", agent: "a b", taskId: "x/y" });
    expect(parseRoute(agentPath("a b"))).toEqual({ page: "board", agent: "a b" });
  });

  it("knows the review queue, a proposal and a report", () => {
    expect(parseRoute("/review")).toEqual({ page: "review" });
    expect(parseRoute(proposalPath("p-1"))).toEqual({ page: "proposal", proposalId: "p-1" });
    expect(parseRoute(reportPath("discovery", "t/1"))).toEqual({
      page: "report",
      agent: "discovery",
      taskId: "t/1",
    });
  });

  it("treats anything else as not found", () => {
    expect(parseRoute("/agents")).toEqual({ page: "missing" });
    expect(parseRoute("/tasks/new")).toEqual({ page: "missing" });
    expect(parseRoute("/agents/%E0%A4%A")).toEqual({ page: "missing" });
    expect(parseRoute("/proposals")).toEqual({ page: "missing" });
    expect(parseRoute("/review/x")).toEqual({ page: "missing" });
  });
});

describe("signinNotice", () => {
  it("has a fixed message for each code the backend sends", () => {
    for (const code of ["expired", "refused", "failed"]) {
      expect(signinNotice(`?signin=${code}`)).toMatch(/\w/);
    }
  });

  it("never echoes an unknown code", () => {
    expect(signinNotice("?signin=<script>")).toBeNull();
    expect(signinNotice("")).toBeNull();
  });
});
