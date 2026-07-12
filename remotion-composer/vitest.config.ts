import { defineConfig } from "vitest/config";

// Node environment by default (pure model/adapter/operation logic).
// Files that need a DOM opt in with a `// @vitest-environment jsdom` pragma.
export default defineConfig({
  test: {
    globals: true,
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    reporters: ["default"],
  },
});
