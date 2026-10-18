import { describe, expect, it } from "vitest";

import { buildQuery, pageParam } from "./query";

describe("buildQuery", () => {
  it("keeps only allowed, non-empty keys and repeats arrays", () => {
    const params = { q: "acme", evil: "x", has_email: "", research_status: ["pending", "needs_review"] };
    expect(buildQuery(params, ["q", "has_email", "research_status"])).toBe(
      "?q=acme&research_status=pending&research_status=needs_review",
    );
  });

  it("applies overrides and encodes values", () => {
    expect(buildQuery({ q: "a&b", page: "3" }, ["q", "page"], { page: "1" })).toBe("?q=a%26b&page=1");
  });

  it("returns an empty string when nothing remains", () => {
    expect(buildQuery({}, ["q"])).toBe("");
  });
});

describe("pageParam", () => {
  const cases: [string | string[] | undefined, number][] = [
    [undefined, 1], ["2", 2], ["0", 1], ["-4", 1], ["abc", 1], [["5", "6"], 5],
  ];
  it.each(cases)("%s gives %s", (input, expected) => {
    expect(pageParam(input)).toBe(expected);
  });
});
