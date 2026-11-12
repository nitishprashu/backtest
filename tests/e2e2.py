"""Second browser flow: indicator mode, templates, explorer keys, narrow viewport, reload persistence."""
import sys, json
from playwright.sync_api import sync_playwright
URL, OUT = sys.argv[1], sys.argv[2]
errs = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    pg.on("console", lambda m: errs.append(("console." + m.type, m.text)) if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errs.append(("pageerror", str(e))))
    pg.goto(URL); pg.wait_for_selector(".leg")
    # template: iron condor -> 4 legs
    pg.click(".tpl:has-text('Iron condor')")
    pg.wait_for_timeout(300)
    assert pg.locator(".leg").count() == 4, pg.locator(".leg").count()
    pg.wait_for_selector("table.t td:has-text('S CE')", timeout=20000)
    pg.wait_for_timeout(1200)
    pg.screenshot(path=f"{OUT}/10_condor.png")
    # persistence across reload
    pg.reload(); pg.wait_for_selector(".leg")
    assert pg.locator(".leg").count() == 4, "strategy not persisted"
    # indicator mode
    pg.click(".seg button:has-text('Indicator signals')")
    pg.wait_for_selector("text=Indicator signals")
    pg.screenshot(path=f"{OUT}/11_indicator.png", full_page=True)
    sel = pg.locator(".leg").first.locator("select").nth(1); sel.select_option("%")
    pg.click(".topbar .btn.primary")
    pg.wait_for_selector(".tabs button:text-is('Overview')", timeout=120000)
    pg.wait_for_timeout(800)
    pg.screenshot(path=f"{OUT}/12_indicator_results.png", full_page=True)
    kp = pg.inner_text(".grid.g6 >> nth=1")
    print("indicator KPI row 2:", kp.replace("\n", " | ")[:200])
    # explorer + keyboard
    pg.click(".nav:has-text('Explorer')")
    pg.wait_for_selector("h3:text-is('Executions')", timeout=30000)
    d1 = pg.locator("select").first.input_value()
    pg.keyboard.press("ArrowLeft"); pg.wait_for_timeout(1500)
    d2 = pg.locator("select").first.input_value()
    print("explorer date", d1, "->", d2)
    assert d1 != d2
    pg.screenshot(path=f"{OUT}/13_explorer_indicator.png", full_page=True)
    # narrow viewport
    pg.set_viewport_size({"width": 1000, "height": 900})
    pg.click(".nav:has-text('Strategy')"); pg.wait_for_selector(".leg"); pg.wait_for_timeout(600)
    pg.screenshot(path=f"{OUT}/14_narrow.png")
    b.close()
print("ERRORS:", json.dumps(errs, indent=1) if errs else "none")
