import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const here = path.dirname(fileURLToPath(import.meta.url));
const rendered = JSON.parse(
  execFileSync("python3", [path.join(here, "render_fixture.py")], {
    cwd: here,
    encoding: "utf8",
  }),
);

async function openFixture(page, name) {
  const consoleProblems = [];
  const network = [];
  page.on("console", (message) => {
    if (["error", "warning"].includes(message.type())) consoleProblems.push(message.text());
  });
  page.on("request", (request) => {
    if (!request.url().startsWith("file:")) network.push(request.url());
  });
  await page.goto(pathToFileURL(rendered[name]).href);
  return { consoleProblems, network };
}

test("full brief is stable, accessible, and reviewable on desktop", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  const observed = await openFixture(page, "full");

  await expect(page).toHaveTitle(/Cleanup that cannot escape its workspace/);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Review brief sections" })).toBeVisible();
  expect(observed.consoleProblems).toEqual([]);
  expect(observed.network).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

  await page.getByRole("link", { name: "Code evidence" }).click();
  await expect(page).toHaveURL(/#evidence$/);
  await expect(page.getByRole("heading", { name: "Focused code evidence" })).toBeInViewport();
  await page.locator(".system-map").focus();
  await expect(page.locator(".system-map")).toBeFocused();

  const axe = await new AxeBuilder({ page }).analyze();
  expect(axe.violations.filter((item) => ["serious", "critical"].includes(item.impact))).toEqual(
    [],
  );
  await page.goto(pathToFileURL(rendered.full).href);
  await expect(page).toHaveScreenshot("review-brief-desktop.png", { fullPage: true });
});

test("full brief contains wide evidence without clipping the mobile page", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const observed = await openFixture(page, "full");

  expect(observed.consoleProblems).toEqual([]);
  expect(observed.network).toEqual([]);
  expect(await page.evaluate(() => document.body.scrollWidth <= document.documentElement.clientWidth)).toBe(
    true,
  );
  await expect(page.locator(".rail")).toBeHidden();
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.locator(".system-map")).toHaveJSProperty("scrollWidth", 664);
  await expect(page).toHaveScreenshot("review-brief-mobile.png", { fullPage: true });
});

test("compact brief omits absent sections and remains complete without JavaScript", async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 900, height: 800 } });
  const page = await context.newPage();
  await page.goto(pathToFileURL(rendered.compact).href);

  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Reject ambiguous cleanup scope");
  await expect(page.getByRole("heading", { name: "Focused code evidence" })).toBeVisible();
  await expect(page.locator("#scenarios")).toHaveCount(0);
  await expect(page.locator("#map")).toHaveCount(0);
  await expect(page.locator("script")).toHaveCount(0);
  await context.close();
});

test("print rendering keeps narrative hierarchy and wraps code", async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 900 });
  await openFixture(page, "full");
  await page.emulateMedia({ media: "print", colorScheme: "light" });

  await expect(page.locator(".rail")).toBeHidden();
  await expect(page.getByRole("heading", { name: "Before and after" })).toBeVisible();
  expect(await page.locator(".code-text").first().evaluate((node) => getComputedStyle(node).whiteSpace)).toBe(
    "pre-wrap",
  );
});
