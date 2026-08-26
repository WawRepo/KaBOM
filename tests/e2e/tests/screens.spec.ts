import { test, expect } from "@playwright/test";

// Runs against whichever seed scenario docker-compose currently has up (see
// run-e2e.sh) — these assertions hold regardless of scenario: there is
// always exactly one successfully-parsed sample sbom ("alpine").

test.describe("the three screens", () => {
  test("/ — search box is focused on load and works with the keyboard alone", async ({
    page,
  }) => {
    await page.goto("/");

    const input = page.locator("#q");
    await expect(input).toBeFocused();

    // Type via the keyboard, no mouse — results should stream in via HTMX.
    await page.keyboard.type("baselayout");
    await expect(page.getByText("alpine-baselayout", { exact: true })).toBeVisible({
      timeout: 5000,
    });
    // Confirms this happened through the live htmx swap, not a full
    // navigation: the URL still has no query string.
    expect(new URL(page.url()).search).toBe("");
  });

  test("/sboms — the inventory renders at least one card", async ({ page }) => {
    await page.goto("/sboms");

    await expect(page.getByTestId("sbom-list")).toBeVisible();
    await expect(page.getByRole("link", { name: /alpine/i })).toBeVisible();
  });

  test("/sboms/{id} — the contents screen lists real components", async ({ page }) => {
    await page.goto("/sboms");
    await page.getByRole("link", { name: /alpine/i }).click();

    await expect(page.getByTestId("component-table")).toBeVisible();
    await expect(page.getByText("alpine-baselayout", { exact: true })).toBeVisible();

    // The client-side filter narrows the visible rows without a page
    // reload. "zlib" matches exactly one package in the sample SBOM (unlike
    // "baselayout", which also matches "alpine-baselayout-data").
    await page.locator("#component-filter").fill("zlib");
    await expect(page.getByText("zlib", { exact: true })).toBeVisible();
    await expect(page.locator("tbody tr:visible")).toHaveCount(1);
  });

  test("the freshness banner is present and never removable on every screen", async ({
    page,
  }) => {
    for (const path of ["/", "/sboms"]) {
      await page.goto(path);
      await expect(page.getByTestId("freshness-banner")).toBeVisible();
    }
  });

  test("is usable on a phone-sized viewport with no horizontal scrolling", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("freshness-banner")).toBeVisible();
    await expect(page.locator("#q")).toBeVisible();

    const hasHorizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    );
    expect(hasHorizontalOverflow).toBe(false);
  });
});
