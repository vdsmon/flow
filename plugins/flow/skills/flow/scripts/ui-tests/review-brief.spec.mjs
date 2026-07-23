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
    const url = request.url();
    if (!url.startsWith("file:") && !url.startsWith("data:")) network.push(url);
  });
  await page.goto(pathToFileURL(rendered[name]).href);
  return { consoleProblems, network };
}

test("full brief is stable, accessible, and reviewable on desktop", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 1000 });
  const observed = await openFixture(page, "full");

  await expect(page).toHaveTitle(/Cleanup that cannot escape its workspace/);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Review brief sections" })).toBeVisible();
  expect(observed.consoleProblems).toEqual([]);
  expect(observed.network).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(await page.locator(".content").evaluate((node) => node.getBoundingClientRect().width)).toBeGreaterThan(
    1100,
  );

  expect(await page.locator(".fold details").count()).toBeGreaterThan(5);
  expect(await page.locator(".fold details[open]").count()).toBe(0);
  await expect(page.locator("#scenarios .scenario.before")).toBeHidden();
  await page.locator("#scenarios summary").click();
  await expect(page.locator("#scenarios details")).toHaveAttribute("open", "");
  await expect(page.locator("#scenarios .scenario.before")).toBeVisible();
  await page.evaluate(() => document.querySelectorAll(".fold details").forEach((node) => (node.open = true)));

  const typeScale = await page.evaluate(() => {
    const size = (selector) => Number.parseFloat(getComputedStyle(document.querySelector(selector)).fontSize);
    return {
      title: size("h1"),
      lead: size(".deck"),
      narrative: size(".observation"),
      card: size(".claim p"),
      scenario: size(".step"),
      check: size(".check p"),
      list: size(".plain-list li"),
      code: size(".code-line"),
      sidebar: size(".rail a"),
      mapLabel: size(".map-node .label"),
      mapKind: size(".map-node .kind"),
    };
  });
  expect(typeScale).toEqual({
    title: 48,
    lead: 20,
    narrative: 18,
    card: 16,
    scenario: 16,
    check: 16,
    list: 16,
    code: 15,
    sidebar: 13,
    mapLabel: 15,
    mapKind: 12,
  });
  await expect(page.locator("footer")).toHaveCount(0);
  await expect(page.locator(".code-line.added .diff-marker")).toContainText("+");
  await expect(page.locator(".code-line.deleted .diff-marker")).toContainText("-");
  const diffBackgrounds = await page.evaluate(() => ({
    added: getComputedStyle(document.querySelector(".code-line.added")).backgroundColor,
    deleted: getComputedStyle(document.querySelector(".code-line.deleted")).backgroundColor,
  }));
  expect(diffBackgrounds.added).not.toBe(diffBackgrounds.deleted);
  await expect(page.locator(".code-line.decisive")).toHaveCount(0);

  const diffGeometry = await page.locator(".code-scroll").first().evaluate((scroll) => {
    const wrapper = scroll.querySelector(".code-lines");
    const rows = [...scroll.querySelectorAll(".code-line")];
    const boxes = rows.map((row) => row.getBoundingClientRect());
    return {
      clientWidth: scroll.clientWidth,
      scrollWidth: scroll.scrollWidth,
      wrapperWidth: wrapper.getBoundingClientRect().width,
      rowWidths: boxes.map((box) => box.width),
      rowHeights: boxes.map((box) => box.height),
      rowMargins: rows.map((row) => {
        const style = getComputedStyle(row);
        return [style.marginTop, style.marginBottom];
      }),
      gaps: boxes.slice(1).map((box, index) => box.top - boxes[index].bottom),
    };
  });
  expect(diffGeometry.scrollWidth).toBeGreaterThan(diffGeometry.clientWidth);
  expect(diffGeometry.rowHeights.every((height) => height === 27)).toBe(true);
  expect(diffGeometry.rowMargins.every((margins) => margins.every((value) => value === "0px"))).toBe(true);
  expect(diffGeometry.gaps.every((gap) => gap === 0)).toBe(true);
  expect(diffGeometry.rowWidths.every((width) => width === diffGeometry.wrapperWidth)).toBe(true);

  const labelsFit = await page.locator(".map-node").evaluateAll((nodes) =>
    nodes.every((node) => {
      const boundary = node.querySelector("rect").getBoundingClientRect();
      return [...node.querySelectorAll(".label tspan")].every((line) => {
        const box = line.getBoundingClientRect();
        return box.right <= boundary.right + 1 && box.bottom <= boundary.bottom + 1;
      });
    }),
  );
  expect(labelsFit).toBe(true);

  const rail = page.locator(".rail");
  const railToggle = page.locator(".rail-disclosure summary");
  await expect(railToggle).toHaveAccessibleName("Collapse navigation");
  expect((await railToggle.boundingBox()).width).toBeLessThanOrEqual(40);
  const toggleLabelBox = await page.getByText("Collapse navigation", { exact: true }).boundingBox();
  expect(toggleLabelBox.width).toBeLessThanOrEqual(1);
  expect(toggleLabelBox.height).toBeLessThanOrEqual(1);
  await page.evaluate(() => scrollTo(0, document.body.scrollHeight / 2));
  await expect.poll(async () => Math.round((await rail.boundingBox()).y)).toBe(0);
  const expandedContentX = (await page.locator(".content").boundingBox()).x;
  await railToggle.click();
  await expect(page.locator(".rail-disclosure")).not.toHaveAttribute("open", "");
  await expect(page.locator(".rail-inner")).toBeHidden();
  await expect(railToggle).toHaveAccessibleName("Expand sections");
  expect((await page.locator(".content").boundingBox()).x).toBeLessThan(expandedContentX);
  await railToggle.click();
  await expect(page.locator(".rail-disclosure")).toHaveAttribute("open", "");

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
  expect(
    await page
      .getByRole("heading", { level: 1 })
      .evaluate((node) => Number.parseFloat(getComputedStyle(node).fontSize)),
  ).toBe(36);
  await expect(page.locator("footer")).toHaveCount(0);
  await expect(page).toHaveScreenshot("review-brief-mobile.png", { fullPage: true });
  await page.evaluate(() => document.querySelectorAll(".fold details").forEach((node) => (node.open = true)));
  expect(await page.locator(".system-map").evaluate((node) => node.scrollWidth > node.clientWidth)).toBe(true);
});

test("Portuguese authored prose localizes all renderer-owned chrome", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  const observed = await openFixture(page, "portuguese");

  expect(observed.consoleProblems).toEqual([]);
  expect(observed.network).toEqual([]);
  await expect(page.locator("html")).toHaveAttribute("lang", "pt-BR");
  await expect(page.getByRole("navigation", { name: "Seções do resumo de revisão" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "A fatia relevante do sistema" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Evidências focadas no código" })).toBeVisible();
  await expect(page.getByText("Nesta página")).toBeVisible();
  await expect(page.getByText("Focused code evidence")).toHaveCount(0);
});

test("compact brief omits absent sections and remains complete without JavaScript", async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 900, height: 800 } });
  const page = await context.newPage();
  await page.goto(pathToFileURL(rendered.compact).href);

  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Reject ambiguous cleanup scope");
  await expect(page.getByRole("heading", { name: "Focused code evidence" })).toBeVisible();
  await expect(page.locator("#evidence > details")).toHaveAttribute("open", "");
  await expect(page.locator(".excerpt")).toHaveAttribute("open", "");
  await expect(page.locator(".code-line").first()).toBeVisible();
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
