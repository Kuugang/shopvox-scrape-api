import re
from typing import List, Tuple

from dotenv import load_dotenv
from playwright.async_api import Page

from helpers import _click_and_wait_domcontent, require_env
from schemas import Item, SizeItem

load_dotenv()
URL_S_AND_S = "https://www.ssactivewear.com"


S_AND_S_USERNAME = require_env("S_AND_S_USERNAME")
S_AND_S_PASSWORD = require_env("S_AND_S_PASSWORD")


# --- helpers ---------------------------------------------------------------
def _parse_int(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0


async def _ensure_warehouse_view(page: Page) -> None:
    """
    S&S uses a custom view switcher, not a <select>. Click to open and pick "Warehouse View".
    Safe to call repeatedly.
    """
    # Wrapper exists on desktop
    wrapper = page.locator("#M_M_zOrderProfileWrapper")
    if not await wrapper.count():
        return  # mobile or already correct; proceed

    try:
        custom_select = wrapper.locator(".custom-select")
        if await custom_select.count():
            await custom_select.click()

        await wrapper.locator("div:has-text('Warehouse View')").first.click()
    except Exception:
        pass


async def _wait_for_grid(page: Page) -> None:
    await page.locator("#M_M_zGrid").wait_for(state="visible", timeout=15000)
    await page.locator("#M_M_zGrid .gR[id^='wh_']").first.wait_for(
        state="visible", timeout=15000
    )


async def _get_size_order(page: Page) -> List[str]:
    spans = await page.locator("#M_M_zGrid .gH span").all_text_contents()
    order = [s.strip() for s in spans if s.strip()]
    return [s for s in order if s.lower() != "color"]


async def _fill_sizes_across_warehouses(
    page: Page, sizes: List["SizeItem"]
) -> Tuple[bool, List[str]]:
    any_added = False
    oos: List[str] = []

    size_order = await _get_size_order(page)
    rows = page.locator("#M_M_zGrid .gR[id^='wh_']")
    row_count = await rows.count()
    if row_count == 0:
        return False, [s.size for s in sizes]

    for si in sizes:
        requested = int(si.quantity)
        remaining = requested

        if si.size not in size_order:
            oos.append(si.size)
            continue

        col_index = size_order.index(si.size)

        for r in range(row_count):
            cell = rows.nth(r).locator("div.i").nth(col_index)

            if await cell.count() == 0:
                continue

            cell_text = (await cell.inner_text()) or ""
            nums = re.findall(r"\d[\d,]*", cell_text)
            available = _parse_int(nums[-1]) if nums else 0
            if available <= 0 or remaining <= 0:
                continue

            take = min(remaining, available)

            qty_input = cell.locator("input[aria-label='quantity']")
            if await qty_input.count() == 0:
                continue

            await qty_input.scroll_into_view_if_needed()
            await qty_input.fill(str(take))

            remaining -= take
            any_added = True
            if remaining == 0:
                break

        if remaining > 0:
            # Not fully satisfied across all warehouses
            oos.append(si.size)

    return any_added, oos


async def process_item(page: Page, item: Item) -> Tuple[bool, List[str]]:

    await search_item(page, item)
    await page.wait_for_load_state("load")

    await choose_color(page, item)

    await _ensure_warehouse_view(page)
    await _wait_for_grid(page)

    any_added, oos = await _fill_sizes_across_warehouses(page, item.sizes)

    if any_added:
        add_btn = page.locator("#aToCDesk, #aToCMobile").first
        if await add_btn.count():
            try:
                await add_btn.click()
            except TimeoutError:
                await add_btn.scroll_into_view_if_needed()
                await add_btn.click()

    return any_added, oos


async def accept_cookies(page: Page):

    await page.set_viewport_size({"width": 1366, "height": 900})
    await page.context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})

    await page.goto(URL_S_AND_S)
    await page.wait_for_load_state("load")

    html = await page.content()
    print(html)
    await page.locator("button#onetrust-accept-btn-handler").click()


async def login(page: Page):

    await page.set_viewport_size({"width": 1366, "height": 900})
    await page.context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})

    await page.goto(URL_S_AND_S + "/myaccount/login")
    await page.locator("input#M_M_zEmailTB").fill(S_AND_S_USERNAME)
    await page.locator("input#M_M_zPasswordTB").fill(S_AND_S_PASSWORD)
    await page.locator("input#M_M_zPageLoginBTN").click()


async def home(page: Page):
    await page.goto(URL_S_AND_S, wait_until="domcontentloaded")


async def search_item(page: Page, item: Item) -> None:
    await page.locator("input[name='M$zSearchTBNew']").fill(item.part)
    search = page.locator("input[name='M$zSearchBTNNew']")
    await _click_and_wait_domcontent(page, search)
    product_a_tag = page.locator("a#gLink0")
    product_href = await product_a_tag.get_attribute("href")
    if product_href:
        await page.goto(
            URL_S_AND_S + product_href,
            wait_until="domcontentloaded",
        )


async def choose_color(page: Page, item: Item) -> None:
    await page.locator(f"div#colorSwatch a:has-text('{item.color}')").click()
