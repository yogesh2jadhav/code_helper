import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../services/api";

const reply = (status: number, body: unknown) =>
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(body), { status }));

afterEach(() => vi.restoreAllMocks());

describe("api client", () => {
  it("builds query strings and skips empty values", async () => {
    const spy = reply(200, []);
    await api.callers("m1", 2);
    await api.source("f1", 5);
    await api.searchMethods("r1", "ship fee");
    expect(spy.mock.calls.map((c) => c[0])).toEqual([
      "/api/methods/m1/callers?depth=2",
      "/api/source/f1?start=5",
      "/api/repositories/r1/methods?q=ship+fee",
    ]);
  });
  it("posts JSON bodies", async () => {
    const spy = reply(200, {});
    await api.why("m1", 8, 10, false);
    const [url, init] = spy.mock.calls[0];
    expect(url).toBe("/api/methods/m1/why");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ start_line: 8, end_line: 10, use_llm: false });
  });
  it("turns error responses into ApiError with the server's message", async () => {
    reply(404, { detail: "unknown method" });
    await expect(api.knowledge("nope")).rejects.toMatchObject({ status: 404, message: "unknown method" });
  });
  it("explains an unreachable backend", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));
    const err = await api.health().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).message).toContain("cannot reach the backend");
    expect((err as ApiError).status).toBe(0);
  });
});
