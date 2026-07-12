import { describe, it, expect } from "vitest";
import { blockerOptionIntent } from "./BrainPanel";

// Regression: a "Retry cancellation" blocker option must re-attempt the CANCEL,
// not retry the stage. `cancel` outranks `retry`.
describe("blockerOptionIntent", () => {
  it("routes a cancel-retry option to cancel, not retry", () => {
    expect(blockerOptionIntent("Retry cancellation")).toBe("cancel");
    expect(blockerOptionIntent("Cancel run")).toBe("cancel");
  });
  it("routes a plain retry option to retry", () => {
    expect(blockerOptionIntent("Retry")).toBe("retry");
    expect(blockerOptionIntent("Retry stage")).toBe("retry");
  });
  it("routes a resume option to resume", () => {
    expect(blockerOptionIntent("Resume")).toBe("resume");
  });
  it("returns null for unknown/unsupported options", () => {
    expect(blockerOptionIntent("Contact support")).toBeNull();
    expect(blockerOptionIntent("Configure provider")).toBeNull();
    expect(blockerOptionIntent("")).toBeNull();
  });
});
