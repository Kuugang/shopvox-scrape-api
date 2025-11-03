import os
from typing import Any, Dict, List, Mapping

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


async def _close_page(page: Page):
    try:
        context = page.context
        pages = context.pages
        if len(pages) > 1:
            await page.close()
        else:
            await page.bring_to_front()
    except Exception:
        pass


def _clean(s: str | None) -> str:
    return (s or "").strip()


def _as_mapping(x: Any) -> Mapping[str, Any]:
    return x if isinstance(x, Mapping) else {}


def _as_list(x: Any) -> List[Any]:
    return x if isinstance(x, list) else []


def _safe_str(x: Any) -> str:
    return "" if x is None else str(x)


def _safe_remove(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"Cleanup failed for {path}: {e}")
