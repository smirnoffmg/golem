import { describe, expect, it } from "vitest";
import { newNonce, since } from "./text";

describe("newNonce", () => {
  it("draws at least 128 random bits in the form the backend accepts", () => {
    const nonce = newNonce();

    expect(nonce).toMatch(/^[A-Za-z0-9_-]{22,64}$/);
    expect(nonce.length * 6).toBeGreaterThanOrEqual(128);
    expect(newNonce()).not.toBe(nonce);
  });
});

describe("since", () => {
  const now = new Date("2026-09-28T12:00:00Z");

  it("says how long ago, coarsely", () => {
    expect(since("2026-09-28T11:59:50Z", now)).toBe("just now");
    expect(since("2026-09-28T11:55:00Z", now)).toBe("5 min ago");
    expect(since("2026-09-28T09:00:00Z", now)).toBe("3 h ago");
    expect(since("2026-09-25T12:00:00Z", now)).toBe("3 d ago");
  });

  it("does not break on a timestamp it cannot read", () => {
    expect(since("not a date", now)).toBe("");
  });
});
