import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  testMatch: ["sidebar-browser.spec.ts", "mobile-app.spec.ts"],
  workers: 1,
  retries: 0,
  reporter: "line",
  use: { baseURL: "http://127.0.0.1:4175", trace: "retain-on-failure" },
  webServer: {
    command: "npx vite --host 127.0.0.1 --port 4175 --strictPort",
    url: "http://127.0.0.1:4175/tests/sidebar-browser.html",
    reuseExistingServer: false,
  },
  projects: [
    { name: "webkit", testMatch: "mobile-app.spec.ts", grepInvert: /desktop keeps|tablet /, use: { ...devices["iPhone 15"] } },
    // Safari's runner can stall after 64 serial browser contexts. Keep the
    // growing drawer suite in its own process without reducing coverage.
    { name: "webkit-sidebar", testMatch: "sidebar-browser.spec.ts", use: { ...devices["iPhone 15"] } },
    { name: "chromium", grepInvert: /desktop keeps|tablet /, use: { ...devices["Pixel 7"] } },
    { name: "ipad-webkit", grep: /tablet |both agents|new chat selects|large activity|windowed sidebar|background token rerenders/, use: { ...devices["Desktop Safari"], viewport:{width:1180,height:820}, hasTouch:true } },
    { name: "desktop", testMatch: "mobile-app.spec.ts", grep: /desktop keeps|both agents Amo/, use: { ...devices["Desktop Chrome"] } },
  ],
});
