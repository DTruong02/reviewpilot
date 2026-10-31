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
});
