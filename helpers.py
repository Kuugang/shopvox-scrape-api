import os

from playwright.async_api import Error, Locator, Page


async def _click_and_wait_domcontent(
    page: Page, locator: Locator, timeout: int = 15000
):
    try:
        async with page.expect_navigation(
            wait_until="domcontentloaded", timeout=timeout
        ):
            await locator.click()
    except Error:
        await page.wait_for_timeout(300)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value
