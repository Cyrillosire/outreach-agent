import { describe, expect, it } from "vitest";

import { ApiError, readCookie, unwrap } from "./envelope";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

describe("unwrap", () => {
  it("returns the data payload of a success envelope", async () => {
    await expect(unwrap(json(200, { data: { a: 1 }, meta: {} }))).resolves.toEqual({ a: 1 });
  });

  it("returns undefined for 204", async () => {
    await expect(unwrap(new Response(null, { status: 204 }))).resolves.toBeUndefined();
  });

  it("throws a typed ApiError for error envelopes", async () => {
    const promise = unwrap(
      json(403, { error: { code: "csrf_failed", message: "Missing or invalid CSRF token", request_id: "r1" } }),
    );
    await expect(promise).rejects.toBeInstanceOf(ApiError);
    await expect(promise).rejects.toMatchObject({ status: 403, code: "csrf_failed", requestId: "r1" });
  });

  it("normalizes non-JSON gateway failures", async () => {
    await expect(unwrap(new Response("<html>Bad Gateway</html>", { status: 502 }))).rejects.toMatchObject({
      code: "backend_unavailable",
    });
  });

  it("rejects successful responses that are not envelopes", async () => {
    await expect(unwrap(json(200, [1, 2]))).rejects.toMatchObject({ code: "malformed_response" });
  });
});

describe("readCookie", () => {
  it("finds a cookie among several and decodes it", () => {
    expect(readCookie("a=1; oa_csrf=abc%3D%3D; b=2", "oa_csrf")).toBe("abc==");
  });

  it("does not match on prefixes", () => {
    expect(readCookie("oa_csrf_old=x", "oa_csrf")).toBeNull();
  });

  it("keeps '=' inside values", () => {
    expect(readCookie("t=a=b", "t")).toBe("a=b");
  });
});
