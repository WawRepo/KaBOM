import { defineConfig, devices } from "@playwright/test";

// Runs against the docker-compose stack (KaBOM + seeded MinIO) — never the
// real storage-host MinIO. See run-e2e.sh, which brings that stack up before
// invoking `playwright test` and tears it down after, and the README for
// how to run this by hand.
export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: process.env.KABOM_BASE_URL || "http://localhost:8090",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    // "Pixel 5" rather than an iPhone preset: same mobile viewport/touch
    // checks, but on Chromium so the suite only needs one installed
    // browser engine (`npx playwright install chromium`) instead of also
    // pulling in WebKit just for this project.
    { name: "mobile", use: { ...devices["Pixel 5"] } },
  ],
});
