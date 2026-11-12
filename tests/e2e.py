"""Browser end-to-end test (Playwright/Chromium) against the app served on the in-memory market."""
import sys, json
from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799/"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/shots"
errs = []

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    pg.on("console", lambda m: errs.append(("console." + m.type, m.text)) if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errs.append(("pageerror", str(e))))
    pg.on("requestfailed", lambda r: errs.append(("requestfailed", r.url)))
    pg.goto(URL)
    pg.wait_for_selector(".leg", timeout=15000)
    pg.wait_for_timeout(1500)
    # make the first leg stop-loss 25% on both legs, then run
    for i in range(2):
        sel = pg.locator(".leg").nth(i).locator("select").nth(1)   # stop-loss kind
        sel.select_option("%")
    pg.wait_for_timeout(300)
    pg.screenshot(path=f"{OUT}/02_builder_sl.png")
    pg.click(".topbar .btn.primary")
    pg.wait_for_selector(".runbanner", timeout=10000)
    pg.wait_for_selector(".tabs button:text-is('Overview')", timeout=120000)
    pg.wait_for_timeout(1200)
    print("results url hash:", pg.evaluate("location.hash"))
    pg.screenshot(path=f"{OUT}/03_results_overview.png", full_page=True)
    for tab in ["Calendar", "Breakdown", "Trades", "Risk", "Diagnostics"]:
        pg.click(f".tabs button:text-is('{tab}')")
        pg.wait_for_timeout(900)
        pg.screenshot(path=f"{OUT}/04_results_{tab.lower()}.png", full_page=True)
    # explorer
    pg.click(".nav:has-text('Explorer')")
    pg.wait_for_selector(".card h3:text-is('Executions')", timeout=30000)
    pg.wait_for_timeout(800)
    pg.screenshot(path=f"{OUT}/05_explorer.png", full_page=True)
    # optimizer
    pg.click(".nav:has-text('Optimizer')")
    pg.wait_for_selector("button:has-text('Run sweep')")
    pg.click("button:has-text('Run sweep')")
    pg.wait_for_selector("h3:text-is('Top 10 by net P&L')", timeout=180000)
    pg.wait_for_timeout(800)
    pg.screenshot(path=f"{OUT}/06_optimizer.png", full_page=True)
    # runs + data
    pg.click(".nav:has-text('Runs')")
    pg.wait_for_selector(".runcard")
    pg.wait_for_timeout(500)
    pg.screenshot(path=f"{OUT}/07_runs.png", full_page=True)
    pg.click(".nav:has-text('Data')")
    pg.wait_for_selector("h3:has-text('Session timing check')", timeout=30000)
    pg.wait_for_timeout(500)
    pg.screenshot(path=f"{OUT}/08_data.png", full_page=True)
    b.close()
print("ERRORS:", json.dumps(errs, indent=1) if errs else "none")
