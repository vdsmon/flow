import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: "review-brief.spec.mjs",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? "github" : "line",
  snapshotPathTemplate: "{testDir}/golden/{arg}{ext}",
  use: {
    browserName: "chromium",
    colorScheme: "light",
    locale: "en-US",
    timezoneId: "UTC",
  },
  expect: {
    toHaveScreenshot: {
      animations: "disabled",
      maxDiffPixelRatio: 0.006,
      scale: "css",
    },
  },
});
