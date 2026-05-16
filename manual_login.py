"""
Manual hh.ru login via VNC.
1. Starts Xvfb virtual display
2. Opens Playwright Chromium (headed) with saved cookies
3. Navigates to hh.ru login page
4. Waits for you to login via VNC
5. Saves cookies when done
"""
import asyncio
import json
import subprocess
import os
import time
from pathlib import Path


async def main():
    # Start Xvfb
    os.environ["DISPLAY"] = ":99"
    xvfb = subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1280x720x24"])
    time.sleep(1)

    # Start x11vnc with password
    vnc = subprocess.Popen(
        [
            "x11vnc", "-display", ":99", "-forever",
            "-passwd", "hh2026", "-rfbport", "5900",
            "-noxdamage",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)

    print("=" * 50)
    print("VNC ready! Connect:")
    print("  Address: 138.16.160.99:5900")
    print("  Password: hh2026")
    print("=" * 50)

    from playwright.async_api import async_playwright

    storage_path = Path("data/browser_sessions/hh_state.json")

    pw = await async_playwright().start()

    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]

    browser = await pw.chromium.launch(headless=False, args=launch_args)

    ctx_opts = {
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1200, "height": 680},
        "locale": "ru-RU",
        "timezone_id": "Europe/Moscow",
    }
    if storage_path.exists():
        ctx_opts["storage_state"] = str(storage_path)
        print(f"Loaded cookies from {storage_path}")

    ctx = await browser.new_context(**ctx_opts)
    await ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = {runtime: {}};
    """)

    page = await ctx.new_page()
    await page.goto("https://hh.ru/account/login", wait_until="domcontentloaded")

    print("\nBrowser open on hh.ru login page")
    print("Login via VNC, script will detect it automatically...")

    while True:
        await asyncio.sleep(5)
        try:
            current = await page.query_selector('[data-qa="mainmenu_applicantProfile"]')
            if not current:
                current = await page.query_selector('[data-qa="mainmenu_myResumes"]')
            if not current:
                current = await page.query_selector('a[href*="/applicant/resumes"]')
            if current:
                print("\nLogin detected! Saving cookies...")
                state = await ctx.storage_state()
                storage_path.parent.mkdir(parents=True, exist_ok=True)
                storage_path.write_text(json.dumps(state), encoding="utf-8")
                print(f"Cookies saved to {storage_path}")
                await page.goto("https://hh.ru", wait_until="domcontentloaded")
                await asyncio.sleep(3)
                state = await ctx.storage_state()
                storage_path.write_text(json.dumps(state), encoding="utf-8")
                print("Final save done")
                break
        except Exception:
            pass

    await asyncio.sleep(3)
    await browser.close()
    await pw.stop()

    xvfb.terminate()
    vnc.terminate()
    print("\nDone! Restart bot: systemctl restart job-hunter")


if __name__ == "__main__":
    asyncio.run(main())
