import { test, expect } from "@playwright/test";

// HOME-232's acceptance criterion: "renders correctly with the network's
// JavaScript blocked for HTMX — degrade to a plain form submit rather than a
// dead box." We block the request for the htmx script itself (rather than
// disabling the whole browser's JS engine, which Playwright can't do via
// context options and page.goto together) — from the page's point of view
// this is exactly "HTMX unavailable": every hx-* attribute becomes inert,
// and kabom/templates/index.html's <form> is a plain method="get" form.

test.describe("degradation when the htmx script is blocked", () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/static/js/htmx.min.js", (route) => route.abort());
  });

  test("the search box is a real form, not a dead control, without htmx", async ({ page }) => {
    await page.goto("/");

    // htmx never loaded: window.htmx must be undefined.
    const htmxLoaded = await page.evaluate(() => "htmx" in window);
    expect(htmxLoaded).toBe(false);

    const input = page.locator("#q");
    await expect(input).toBeFocused();

    await input.fill("baselayout");
    // Enter submits the plain <form method="get" action="/">, a full
    // navigation to "/?q=baselayout" — not an ajax call.
    await Promise.all([page.waitForURL(/\/\?q=baselayout/), input.press("Enter")]);

    await expect(page.getByText("alpine-baselayout", { exact: true })).toBeVisible();
  });

  test("the visible Search button also works as a plain submit", async ({ page }) => {
    await page.goto("/");

    await page.locator("#q").fill("baselayout");
    await Promise.all([
      page.waitForURL(/\/\?q=baselayout/),
      page.getByRole("button", { name: "Search" }).click(),
    ]);

    await expect(page.getByText("alpine-baselayout", { exact: true })).toBeVisible();
  });
});
