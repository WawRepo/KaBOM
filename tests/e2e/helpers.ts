import { expect, type Page } from "@playwright/test";

// Fixed, dev-only credentials matching docker-compose.yml's `kabom`
// service — never a real credential, never valid against anything but this
// compose stack.
export const DEV_USERNAME = "kabom-dev";
export const DEV_PASSWORD = "kabom-dev-only-not-a-real-password";

/**
 * Sign in through the real login form.
 *
 * Deliberately not Playwright's `httpCredentials`: that only sends
 * credentials in response to a `WWW-Authenticate` challenge, and KaBOM
 * never sends one — a browser gets redirected to /login instead of a
 * native popup. Driving the actual form is also the more honest test.
 */
export async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.fill("#username", DEV_USERNAME);
  await page.fill("#password", DEV_PASSWORD);
  await page.click('button[type="submit"]');
  // The form redirects to whatever `next` said, defaulting to the search
  // page; waiting on the search box is what proves we are actually in.
  await expect(page.locator("#q")).toBeVisible();
}
