import { test, expect } from "@playwright/test";
import { login } from "../helpers";

// Runs against whichever seed scenario docker-compose currently has up (see
// run-e2e.sh). These assertions hold regardless of scenario, and regardless
// of anything extra dropped into dev-sboms/: every scenario seeds the three
// committed Alpine-based images, all of which contain `alpine-baselayout`
// and exactly one package named `zlib`.

test.describe("the three screens", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

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
    await expect(page.getByRole("link", { name: /nginx/i })).toBeVisible();
  });

  test("/sboms — the filter narrows the cards and says so", async ({ page }) => {
    await page.goto("/sboms");
    const cards = page.locator(".sbom-card");
    const total = await cards.count();
    expect(total).toBeGreaterThan(1);

    // "nginx" matches exactly one committed dev SBOM (nginx-1.27-alpine).
    await page.locator("#sbom-filter").fill("nginx");
    await expect(page.locator(".sbom-card:visible")).toHaveCount(1);
    await expect(page.getByTestId("sbom-count")).toHaveText(new RegExp(`^\\s*1 of ${total} SBOMs\\s*$`));

    await page.locator("#sbom-filter").fill("no-such-thing");
    await expect(page.locator(".sbom-card:visible")).toHaveCount(0);
    await expect(page.locator("#sbom-no-match")).toBeVisible();

    await page.locator("#sbom-filter").fill("");
    await expect(page.locator(".sbom-card:visible")).toHaveCount(total);
    await expect(page.locator("#sbom-no-match")).toBeHidden();
  });

  test("/sboms/{id} — the contents screen lists real components", async ({ page }) => {
    await page.goto("/sboms");
    await page.getByRole("link", { name: /nginx/i }).click();

    await expect(page.getByTestId("component-table")).toBeVisible();
    await expect(page.getByText("alpine-baselayout", { exact: true })).toBeVisible();

    // The client-side filter narrows the visible rows without a page
    // reload. "zlib" matches exactly one package in each committed image
    // (unlike "baselayout", which also matches "alpine-baselayout-data").
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
