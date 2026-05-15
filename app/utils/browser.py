import json
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
import structlog

from app.config import settings
from app.utils.anti_detect import random_user_agent, random_viewport

log = structlog.get_logger()

STORAGE_DIR = Path("data/browser_sessions")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)


class BrowserManager:
    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._contexts: dict[str, BrowserContext] = {}

    async def start(self):
        self._playwright = await async_playwright().start()
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ]
        self._browser = await self._playwright.chromium.launch(
            headless=settings.browser_headless,
            args=launch_args,
        )
        log.info("browser_started", headless=settings.browser_headless)

    async def get_context(self, platform: str) -> BrowserContext:
        if platform in self._contexts:
            return self._contexts[platform]

        storage_path = STORAGE_DIR / f"{platform}_state.json"
        ctx_opts = {
            "user_agent": random_user_agent(),
            "viewport": random_viewport(),
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
        }
        if settings.proxy_url:
            ctx_opts["proxy"] = {"server": settings.proxy_url}
        if storage_path.exists():
            ctx_opts["storage_state"] = str(storage_path)

        ctx = await self._browser.new_context(**ctx_opts)
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US', 'en']});
            window.chrome = {runtime: {}};
        """)
        self._contexts[platform] = ctx
        log.info("browser_context_created", platform=platform)
        return ctx

    async def save_context(self, platform: str):
        if platform in self._contexts:
            storage_path = STORAGE_DIR / f"{platform}_state.json"
            state = await self._contexts[platform].storage_state()
            storage_path.write_text(json.dumps(state), encoding="utf-8")
            log.info("browser_state_saved", platform=platform)

    async def new_page(self, platform: str) -> Page:
        ctx = await self.get_context(platform)
        return await ctx.new_page()

    async def close(self):
        for platform in list(self._contexts):
            await self.save_context(platform)
            await self._contexts[platform].close()
        self._contexts.clear()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        log.info("browser_closed")


browser_manager = BrowserManager()
