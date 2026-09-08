import { test, expect } from "@playwright/test";
import { DEV_PASSWORD, DEV_USERNAME, login } from "../helpers";

// The login page replaced the browser's native basic-auth popup. These
// tests deliberately do NOT use the shared beforeEach login — they are
// about what happens before you are signed in.

test.describe("signing in", () => {
  test("an unauthenticated visitor is sent to the login form, not a popup", async ({ page }) => {
    const response = await page.goto("/sboms");

    // A native basic-auth popup would have meant a 401 the browser handled
    // itself; instead we land on a real page with real form fields.
    expect(response?.status()).toBe(200);
    expect(new URL(page.url()).pathname).toBe("/login");
    await expect(page.locator("#username")).toBeVisible();
    await expect(page.locator("#password")).toBeVisible();
  });

  test("a wrong password is refused and says so on the page", async ({ page }) => {
    await page.goto("/login");
    await page.fill("#username", DEV_USERNAME);
    await page.fill("#password", "definitely-not-the-password");
    await page.click('button[type="submit"]');

    await expect(page.getByTestId("login-error")).toBeVisible();
    expect(new URL(page.url()).pathname).toBe("/login");
    // Still locked out.
    await page.goto("/sboms");
    expect(new URL(page.url()).pathname).toBe("/login");
  });

  test("signing in returns you to the page you asked for", async ({ page }) => {
    await page.goto("/sboms");
    await page.fill("#username", DEV_USERNAME);
    await page.fill("#password", DEV_PASSWORD);
    await page.click('button[type="submit"]');

    // Back to /sboms, not dumped on the search page.
    await expect(page.getByTestId("sbom-list")).toBeVisible();
    expect(new URL(page.url()).pathname).toBe("/sboms");
  });

  test("signing out ends the session", async ({ page }) => {
    await login(page);

    await page.click('button:has-text("Sign out")');

    await expect(page.locator("#username")).toBeVisible();
    await page.goto("/sboms");
    expect(new URL(page.url()).pathname).toBe("/login");
  });
});
