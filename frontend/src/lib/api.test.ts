import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

describe("API errors", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("extracts the safe message from FastAPI structured details", async () => {
    const response = {
      ok: false,
      status: 404,
      json: async () => ({ detail: { code: "repository_not_found", message: "Repository was not found." } }),
    } as Response;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));

    await expect(api.repositories()).rejects.toThrow("Repository was not found.");
  });

  it("sends the operator token only to validation endpoints", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    vi.stubGlobal("fetch", fetchMock);
    api.setValidationToken("session-only-secret");

    await api.repositories();
    expect(fetchMock.mock.calls[0][1]?.headers).not.toHaveProperty("Authorization");

    await api.validationPlan("finding-id");
    expect(fetchMock.mock.calls[1][1]?.headers).toMatchObject({ Authorization: "Bearer session-only-secret" });
    api.clearValidationToken();
  });
});
