import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PWTimeoutError
from playwright.async_api import expect

from helpers import require_env
from schemas2 import SalesOrder

BASE_URL = "https://express.shopvox.com/"
NEW_SALES_ORDER = BASE_URL + "transactions/sales-orders/new"

TIMEZONE = require_env("TIMEZONE")


async def pick_date_n_days_ahead(page: Page, days_ahead: int):
    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).date()
    target = today + timedelta(days=days_ahead)
    target_month_label = target.strftime("%B %Y")
    target_day_str = str(target.day)

    dialog = page.get_by_role("dialog")
    await expect(dialog).to_be_visible()

    header = dialog.locator("p.css-i7pnfr")
    next_btn = dialog.locator("div.css-12c1il0")

    for _ in range(12):
        current_label = (await header.inner_text()).strip()
        if current_label == target_month_label:
            break
        cur_dt = datetime.strptime(current_label, "%B %Y").date().replace(day=1)
        tgt_dt = target.replace(day=1)
        if (cur_dt.year, cur_dt.month) < (tgt_dt.year, tgt_dt.month):
            await next_btn.click()
            await expect(header).to_have_text(target_month_label)
            break
        else:
            raise RuntimeError(
                "Calendar shows a future month unexpectedly; no prev button handler set."
            )

    day = dialog.locator("div.css-f4l6no > p:not(.css-wgrtxn):not(.css-qfneov)").filter(
        has_text=target_day_str
    )

    await expect(day.first).to_be_visible()
    await day.first.click()


async def create_so(page: Page, order: SalesOrder):
    await page.goto(NEW_SALES_ORDER, wait_until="domcontentloaded")
    await page.wait_for_load_state("load")

    await page.get_by_role("button", name="Show All Fields").click()
    await page.locator(".css-8rytzr-control").first.click()

    await page.get_by_role("combobox", name="* Customer").fill(order.store_name)

    target_store = page.locator(f"div.ml4:has-text('{order.store_name}')")
    await target_store.wait_for(state="visible")
    await target_store.click()

    await page.get_by_test_id("title-input").click()

    await page.get_by_test_id("title-input").fill(order.order_name)

    await page.locator(
        "#dueDate-field-wrapper > ._wrapper_caahe_1 > div > div > div > ._extras_caahe_75 > .f > .f-shrink-0"
    ).click()

    await pick_date_n_days_ahead(page, 10)

    await page.locator("button[type='submit']").click()

    await page.wait_for_load_state("load")
    await page.locator("p.css-fqwlf2:has-text('Customer')").wait_for(state="visible")

    # await page.goto(
    #     "https://express.shopvox.com/transactions/sales-orders/05d26516-198a-484f-b368-062f39a63f46",
    #     wait_until="domcontentloaded",
    # )

    for index, item in enumerate(order.items):
        new_line_item_button = page.locator("button.css-gjgr8x")
        await new_line_item_button.wait_for(state="visible")
        await new_line_item_button.click()
        create_line_item_button = page.locator("button.css-1f4m2s7")
        await create_line_item_button.wait_for(state="visible")
        product_input = page.locator("#productId-input")
        await product_input.fill("APPAREL")
        await page.wait_for_timeout(500)
        apparel_option = (
            page.get_by_role("option").filter(has_text=re.compile(r"^APPAREL$")).first
        )
        await expect(apparel_option).to_be_visible(timeout=10_000)
        await apparel_option.click()
        await page.get_by_test_id("name-input").click()
        await page.get_by_test_id("name-input").press("ControlOrMeta+a")
        await page.get_by_test_id("name-input").fill(str(index))
        await page.locator(
            '[id="apparel.items[0].catalogId-field-wrapper"] > .field-wrapper-container > .f.f-alignItems-c > .f-grow-1 > .css-nxiuxh-container > .css-8rytzr-control > .css-1wy0on6 > .css-1xb41ip-indicatorContainer > .css-8mmkcg'
        ).click()
        # Catalogs
        catalogs = [
            "SanMar",
            "S&S Activewear",
            "Custom",
        ]
        product_name = item.name
        catalog_dropdown = page.locator(
            "#apparel\\.items\\[0\\]\\.catalogId-field-wrapper"
        )
        product_input = page.get_by_role("combobox", name="* Product")
        found = False
        for cat_index, cat in enumerate(catalogs):
            if cat_index == 1:
                await catalog_dropdown.click()
            await page.get_by_role("option", name=cat).click()
            await product_input.click()
            await product_input.fill("")
            await product_input.fill(product_name)
            await page.get_by_role("listbox").wait_for(state="visible")
            option = page.get_by_role("option").first
            try:
                await option.wait_for(state="visible", timeout=5_000)
                await option.click()
                found = True
                break
            except PWTimeoutError:
                continue
        # Color
        await page.locator(
            '[id="apparel.items[0].color-field-wrapper"] > .field-wrapper-container > .f.f-alignItems-c > .f-grow-1 > .css-nxiuxh-container > .css-8rytzr-control > .css-1fc4u07 > .css-cy4hh4'
        ).click()
        await page.get_by_role("combobox", name="* Color").fill(item.color)
        await page.get_by_role("option", name=item.color, exact=True).click()
        # Wait for load
        await page.locator("span.f-shrink-0.css-nk0bb7:has-text('Show More')").wait_for(
            state="visible"
        )

        # Size input
        sizes_container = page.locator("div._apparelItemSizes_tgx96_1").nth(index)
        await expect(sizes_container).to_be_visible()

        row = sizes_container.locator(
            f"div.PricingTemplateApparelItemsItemSizesSize:has(div._apparelItemSizesPricingLabel_tgx96_30:text-is('{item.size}'))"
        ).first
        await expect(row).to_be_visible()

        qty_input = row.locator("input[id$='.quantity-input']").first
        await expect(qty_input).to_be_visible()
        await qty_input.fill(item.quantity)
        await page.locator("button.ml4.css-xdirqf").click()
        await page.locator(
            f"p.fontWeight-b.f-shrink-0.css-i7pnfr:has-text('{index}')"
        ).wait_for(state="visible")
