import { describe, expect, it } from "vitest";

import { ApiError } from "./envelope";
import { errorMessage } from "./errors";
import { linesToList } from "./lists";

describe("errorMessage", () => {
  it("uses the API message for ordinary errors", () => {
    expect(errorMessage(new ApiError(409, { code: "campaign_not_ready", message: "Finish the brief." }), "x"))
      .toBe("Finish the brief.");
  });

  it("names the field of the first validation error", () => {
    const err = new ApiError(422, {
      code: "validation_error", message: "Request validation failed",
      details: [{ loc: ["body", "brief", "style", "max_words"], msg: "Input should be at least 50" }],
    });
    expect(errorMessage(err, "x")).toBe("brief › style › max_words: Input should be at least 50");
  });

  it("falls back for non-API errors", () => {
    expect(errorMessage(new TypeError("network"), "Couldn't save.")).toBe("Couldn't save.");
  });
});

describe("linesToList", () => {
  it("trims lines and drops blanks", () => {
    expect(linesToList("  CIO \n\n digital\n")).toEqual(["CIO", "digital"]);
  });
});
