import { test, expect } from "@playwright/test";

// Which of the three banner colours to expect depends on which scenario
// scripts/seed_minio.py seeded the compose stack with — set by run-e2e.sh
// via KABOM_SEED_SCENARIO before each `docker compose up` + test pass.
// Defaults to "mixed" (docker-compose.yml's own default) so this still does
// something sensible if the file is run without run-e2e.sh.
const scenario = process.env.KABOM_SEED_SCENARIO || "mixed";

test.describe(`freshness banner — scenario "${scenario}"`, () => {
  test("banner shows the expected colour and, when stale, the failure warning", async ({
    page,
  }) => {
    await page.goto("/");
    const banner = page.getByTestId("freshness-banner");
    await expect(banner).toBeVisible();

    if (scenario === "fresh") {
      await expect(banner).toHaveAttribute("data-level", "green");
      await expect(banner).toContainText("Updated");
      await expect(banner).not.toContainText("STALE");
    } else if (scenario === "amber") {
      await expect(banner).toHaveAttribute("data-level", "amber");
      await expect(banner).toContainText("Updated");
      await expect(banner).not.toContainText("STALE");
    } else {
      // "mixed": one good sample + one corrupted sample -> a failed read,
      // which forces RED regardless of age (see kabom/main.py's
      // _freshness_banner).
      await expect(banner).toHaveAttribute("data-level", "red");
      await expect(banner).toContainText("STALE");
      await expect(banner).toContainText("1 of 2 read");
      await expect(banner).toContainText("These answers may be wrong. Check the SBOM job.");
    }
  });

  test("search results carry a visible red border exactly when the banner is red", async ({
    page,
  }) => {
    await page.goto("/?q=baselayout");
    const results = page.getByTestId("search-results");
    await expect(results).toBeVisible();

    const hasRedBorderClass = await results.evaluate((el) => el.classList.contains("border-red-600"));
    const borderWidth = await results.evaluate((el) => getComputedStyle(el).borderWidth);

    if (scenario === "mixed") {
      // border-2 border-red-600 -> a real, non-zero visible border, with the
      // red-600 utility class present (the exact rendered colour value is
      // Tailwind-version-specific — v4 renders it as an oklch() string, not
      // rgb() — so the class name is the stable thing to assert on).
      expect(borderWidth).not.toBe("0px");
      expect(hasRedBorderClass).toBe(true);
    } else {
      expect(borderWidth).toBe("0px");
      expect(hasRedBorderClass).toBe(false);
    }
  });
});
