import { afterEach, describe, expect, it, vi } from "vitest";
import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ReviewFinder } from "./review-finder";
import { api } from "@/lib/api";
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/api")>()), api: { review: vi.fn(), feedback: vi.fn() } }));
const mockReview = { id: "review-uuid", pull_request_id: "pr-uuid", commit_sha: "abcdef123456", status: "completed", created_at: "2026-01-01", findings: [{ id: "finding-1", severity: "high" as const, title: "Unsafe query", explanation: "Input is not checked.", category: "security", file_path: "src/a.py", line_number: 12, feedback: null }, { id: "finding-2", severity: "low" as const, title: "Unused value", explanation: "Remove this.", category: "maintainability", file_path: "src/b.py", line_number: 4, feedback: null }], model_name: "demo", duration_ms: 1200, input_tokens: 10, output_tokens: 5 };
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.useRealTimers(); });
describe("review details", () => {
 it("groups findings by severity and filters them", async () => { vi.mocked(api.review).mockResolvedValue(mockReview); render(React.createElement(ReviewFinder,{id:"review-uuid"})); expect(await screen.findByText("Unsafe query")).toBeInTheDocument(); expect(screen.getByText("high", { selector: "h3" })).toBeInTheDocument(); fireEvent.change(screen.getByLabelText("Filter severity"), { target: { value: "low" } }); expect(screen.queryByText("Unsafe query")).not.toBeInTheDocument(); expect(screen.getByText("Unused value")).toBeInTheDocument(); });
 it("submits feedback", async () => { vi.mocked(api.review).mockResolvedValue(mockReview); vi.mocked(api.feedback).mockResolvedValue({}); render(React.createElement(ReviewFinder,{id:"review-uuid"})); expect(await screen.findByText("Unsafe query")).toBeInTheDocument(); fireEvent.click(screen.getAllByText("Helpful")[0]); expect(api.feedback).toHaveBeenCalledWith("finding-1", "helpful"); });
 it("shows no-findings and a useful API error", async () => { vi.mocked(api.review).mockResolvedValue({ ...mockReview, findings: [] }); const { rerender }=render(React.createElement(ReviewFinder,{id:"review-uuid"})); expect(await screen.findByText("No findings. This review found nothing to report.")).toBeInTheDocument(); vi.mocked(api.review).mockRejectedValue(new Error("Unavailable")); rerender(React.createElement(ReviewFinder,{id:"another-id"})); expect(await screen.findByRole("alert")).toHaveTextContent("Could not load review."); });
});
