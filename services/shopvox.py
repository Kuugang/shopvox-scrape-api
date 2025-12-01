import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PWTimeoutError
from playwright.async_api import expect

from helpers import _normalize_size, require_env
from schemas2 import SalesOrder

BASE_URL = "https://express.shopvox.com/"
NEW_SALES_ORDER = BASE_URL + "transactions/sales-orders/new"

TIMEZONE = require_env("TIMEZONE")

PRESS_SEQUENTIALLY_DELAY = 0


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
    STORE_RETRIES = 5
    ITEM_RETRIES = 2
    custom_items = []

    for retry in range(STORE_RETRIES):
        try:
            await page.goto(NEW_SALES_ORDER, wait_until="domcontentloaded")
            await page.wait_for_load_state("load")

            await page.get_by_role("button", name="Show All Fields").click(
                timeout=300_000
            )
            await page.locator(".css-8rytzr-control").first.click()

            await page.get_by_role("combobox", name="* Customer").fill("")
            await page.get_by_role("combobox", name="* Customer").press_sequentially(
                order.store_name, delay=PRESS_SEQUENTIALLY_DELAY
            )

            target_store = page.locator(f"div.ml4:has-text('{order.store_name}')")
            await target_store.wait_for(state="visible", timeout=5_000)
            await target_store.click()

            break
        except PWTimeoutError:
            if retry == STORE_RETRIES - 1:
                raise RuntimeError("Could not find store")
            continue

    await page.get_by_test_id("title-input").click()

    await page.get_by_test_id("title-input").press_sequentially(
        "TEST " + order.order_name, delay=PRESS_SEQUENTIALLY_DELAY
    )

    await page.locator(
        "#dueDate-field-wrapper > ._wrapper_caahe_1 > div > div > div > ._extras_caahe_75 > .f > .f-shrink-0"
    ).click()

    await pick_date_n_days_ahead(page, 10)

    await page.locator("button[type='submit']").click()

    await page.wait_for_load_state("load")
    await page.locator("p.css-fqwlf2:has-text('Customer')").wait_for(state="visible")
    await page.reload()
    await page.wait_for_load_state("load")
    await page.locator("p.css-fqwlf2:has-text('Customer')").wait_for(state="visible")
    await page.reload()
    await page.wait_for_load_state("load")
    await page.locator("p.css-fqwlf2:has-text('Customer')").wait_for(state="visible")

    # Process each item with retry mechanism
    for index, item in enumerate(order.items):
        for item_retry in range(ITEM_RETRIES):
            try:
                is_custom = await process_line_item(page, index, item)
                if is_custom:
                    custom_items.append({
                        "name": item.name,
                        "color": item.color,
                        "size": item.size,
                        "style": item.style,
                        "quantity": item.quantity,
                        "price": item.price,
                        "total": item.total,
                    })
                break
            except Exception as e:
                if item_retry == ITEM_RETRIES - 1:
                    raise RuntimeError(
                        f"Failed to add item {index} after {ITEM_RETRIES} retries: {str(e)}"
                    )
                print(
                    f"Retry {item_retry + 1}/{ITEM_RETRIES} for item {index}: {str(e)}"
                )

                # Optional: Close any open modals/dropdowns before retrying
                try:
                    await page.locator("div.css-tob1hr").click()
                    await page.wait_for_timeout(500)
                except Exception:
                    pass

                continue

    # # Add not order yet tag
    # await page.wait_for_timeout(200)
    # await page.get_by_role("button", name="Add Tag").click()
    # await page.get_by_role("combobox", name="Tags").press_sequentially(
    #     "NOT ORDER YET", delay=PRESS_SEQUENTIALLY_DELAY
    # )
    # await page.wait_for_timeout(200)
    # await page.locator("div").filter(has_text=re.compile(r"^NOT ORDER YET$")).nth(
    #     1
    # ).click()
    # await page.wait_for_timeout(200)
    #
    # modal = page.locator("#root-modals-dropdowns [role='dialog']").first
    # await modal.wait_for(state="visible", timeout=10_000)
    #
    # submit_btn = modal.locator("button.ml4.css-12lhddq").first
    # if await submit_btn.count() == 0:
    #     submit_btn = modal.locator("button[type='submit']").first
    # await submit_btn.wait_for(state="visible", timeout=10_000)
    # await submit_btn.click()
    # await page.wait_for_timeout(5_000)

    return custom_items


async def process_line_item(page: Page, index: int, item):
    """Process a single line item with all its steps"""
    new_line_item_button = page.locator("button.css-gjgr8x")
    await new_line_item_button.wait_for(state="visible")
    await page.wait_for_timeout(200)
    await new_line_item_button.click()
    create_line_item_button = page.locator("button.css-1f4m2s7")
    await create_line_item_button.wait_for(state="visible")
    product_input = page.locator("#productId-input")
    await product_input.press_sequentially("APPAREL", delay=PRESS_SEQUENTIALLY_DELAY)
    await page.wait_for_timeout(200)
    apparel_option = (
        page.get_by_role("option").filter(has_text=re.compile(r"^APPAREL$")).first
    )
    await expect(apparel_option).to_be_visible(timeout=10_000)
    await page.wait_for_timeout(200)
    await apparel_option.click()
    await page.wait_for_timeout(200)
    await page.get_by_test_id("name-input").click()
    await page.get_by_test_id("name-input").press("ControlOrMeta+a")
    await page.get_by_test_id("name-input").press_sequentially(
        str(index), delay=PRESS_SEQUENTIALLY_DELAY
    )
    await page.wait_for_timeout(200)
    await page.locator(
        '[id="apparel.items[0].catalogId-field-wrapper"] > .field-wrapper-container > .f.f-alignItems-c > .f-grow-1 > .css-nxiuxh-container > .css-8rytzr-control > .css-1wy0on6 > .css-1xb41ip-indicatorContainer > .css-8mmkcg'
    ).click()

    # Catalogs
    catalogs = [
        "SanMar",
        "S&S Activewear",
        "Custom",
    ]
    catalog_dropdown = page.locator("#apparel\\.items\\[0\\]\\.catalogId-field-wrapper")
    product_input = page.get_by_role("combobox", name="* Product")
    found = False
    is_custom = False
    cat_index = 0

    for cat_index, cat in enumerate(catalogs):
        is_custom = cat == "Custom"
        await page.wait_for_timeout(200)
        if cat_index >= 1:
            await catalog_dropdown.click()
        await page.get_by_role("option", name=cat).click()

        if is_custom:
            await page.locator(
                'input[id="apparel.items[0].productName-input"]'
            ).press_sequentially(item.name, delay=PRESS_SEQUENTIALLY_DELAY)
            # For custom, skip the product search and go straight to color
            break

        style_to_match = item.style or item.name
        pattern = rf"\b{re.escape(style_to_match)}\b"

        # Try to find product in this catalog
        product_found = False
        for attempt in range(2):
            try:
                await page.wait_for_timeout(200)
                await product_input.click()
                await product_input.fill("")
                await product_input.press_sequentially(
                    style_to_match, delay=PRESS_SEQUENTIALLY_DELAY
                )
                await page.get_by_role("listbox").wait_for(
                    state="visible", timeout=2_000
                )
                option = (
                    page.get_by_role("option")
                    .filter(has_text=re.compile(pattern))
                    .first
                )
                await option.wait_for(state="visible", timeout=10_000)
                await option.click()
                product_found = True
                break
            except PWTimeoutError:
                if attempt == 2 - 1:
                    break
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass

        # If product not found in this catalog, try next catalog
        if not product_found:
            continue

        # Product found, now try to find color
        await page.wait_for_timeout(5000)

        try:
            await page.locator(
                '[id="apparel.items[0].color-field-wrapper"] > .field-wrapper-container > .f.f-alignItems-c > .f-grow-1 > .css-nxiuxh-container > .css-8rytzr-control > .css-1fc4u07 > .css-cy4hh4'
            ).click(timeout=60_000)
            await page.wait_for_timeout(1000)
            await page.locator("div.css-hvcirc-control").wait_for(
                state="visible", timeout=60_000
            )

            color = item.color
            if "/" in color:
                left, right = color.split("/", 1)
                color = f"{left.rstrip()}/ {right.lstrip()}"

            color_found = False
            for retry in range(3):
                await page.wait_for_timeout(1000)
                try:
                    await page.get_by_role("combobox", name="* Color").fill("")
                    await page.get_by_role(
                        "combobox", name="* Color"
                    ).press_sequentially(color, delay=PRESS_SEQUENTIALLY_DELAY)
                    await page.wait_for_timeout(200)

                    color_option = page.get_by_role("option", name=color, exact=True)
                    await color_option.wait_for(state="visible", timeout=10_000)
                    await color_option.click()

                    color_found = True
                    break
                except PWTimeoutError:
                    if retry == 3 - 1:
                        break
                    continue

            # If color not found, try next catalog
            if not color_found:
                print(
                    f"Color '{color}' not found in catalog '{cat}', trying next catalog..."
                )
                continue

            # Color found, now check if size is available
            # Wait for size container to load
            await page.locator("span.f-shrink-0.css-nk0bb7:has-text('Copy Style')").wait_for(
                state="visible", timeout=30_000
            )
            
            sizes_container = page.locator("div._apparelItemSizes_tgx96_1").nth(index)
            await expect(sizes_container).to_be_visible(timeout=10_000)
            normalized_size = _normalize_size(item.size)

            # Check if the size exists in the standard size inputs
            col = sizes_container.locator(
                f"div.PricingTemplateApparelItemsItemSizesSize:has(div._apparelItemSizesPricingLabel_tgx96_30:text-is('{normalized_size}'))"
            ).first
            
            size_found = False
            try:
                await expect(col).to_be_visible(timeout=2000)
                size_found = True
            except:
                # Check if odd size input is available (for sizes not in standard list)
                odd_size_input = page.locator(
                    f"input#apparel\\.items\\[{index}\\]\\.oddSize\\.size-input"
                )
                try:
                    await expect(odd_size_input).to_be_visible(timeout=2000)
                    size_found = True  # We can use odd size input
                except:
                    size_found = False

            if not size_found:
                print(
                    f"Size '{normalized_size}' not found in catalog '{cat}', trying next catalog..."
                )
                continue

            # Product, color, and size all found - we're done with catalog selection
            found = True
            break

        except Exception as e:
            # Error during color/size selection, try next catalog
            print(
                f"Error finding color/size in catalog '{cat}': {e}, trying next catalog..."
            )
            continue

    # If we exhausted all catalogs and still haven't found it, raise error
    if not found and not is_custom:
        raise RuntimeError("Product/Color/Size not found in any catalog")

    # Handle custom catalog color/style input
    if is_custom:
        await page.wait_for_timeout(5000)
        await page.locator(
            "input[id='apparel.items[0].partNumber-input']"
        ).press_sequentially(item.style, delay=PRESS_SEQUENTIALLY_DELAY)
        await page.locator(
            "input[id='apparel.items[0].color-input']"
        ).press_sequentially(item.color, delay=PRESS_SEQUENTIALLY_DELAY)
        
        # Wait for load for custom items
        await page.locator("span.f-shrink-0.css-nk0bb7:has-text('Copy Style')").wait_for(
            state="visible"
        )

    # Size input (at this point we know the size exists or we're using custom)
    sizes_container = page.locator("div._apparelItemSizes_tgx96_1").nth(index)
    await expect(sizes_container).to_be_visible()
    normalized_size = _normalize_size(item.size)

    col = sizes_container.locator(
        f"div.PricingTemplateApparelItemsItemSizesSize:has(div._apparelItemSizesPricingLabel_tgx96_30:text-is('{normalized_size}'))"
    ).first
    try:
        await expect(col).to_be_visible(timeout=2000)
        # If found, use the standard size input
        qty_input = col.locator("input[id$='.quantity-input']").first
        await expect(qty_input).to_be_visible()
        await qty_input.fill("")
        await qty_input.press_sequentially(
            item.quantity, delay=PRESS_SEQUENTIALLY_DELAY
        )
    except:
        # If not found, use the custom/odd size input
        odd_size_input = page.locator(
            f"input#apparel\\.items\\[{index}\\]\\.oddSize\\.size-input"
        )
        await expect(odd_size_input).to_be_visible()
        await odd_size_input.fill("")
        await odd_size_input.press_sequentially(
            _normalize_size(item.size), delay=PRESS_SEQUENTIALLY_DELAY
        )
        # Wait for quantity input to become enabled after size is filled
        qty_input = page.locator(
            f"input#apparel\\.items\\[{index}\\]\\.oddSize\\.quantity-input"
        )
        await expect(qty_input).to_be_enabled()
        await qty_input.fill("")
        await qty_input.press_sequentially(
            item.quantity, delay=PRESS_SEQUENTIALLY_DELAY
        )
        # Update col to point to the parent container div for the cost input
        col = page.locator(
            f"div.PricingTemplateApparelItemsItemSizesSize:has(input#apparel\\.items\\[{index}\\]\\.oddSize\\.size-input)"
        )

    if is_custom:
        cost_input = col.locator("input[id$='.costInDollars-input']").first
        await expect(cost_input).to_be_enabled()
        await cost_input.fill("")
        await cost_input.press_sequentially(item.price, delay=PRESS_SEQUENTIALLY_DELAY)

    await page.wait_for_timeout(200)
    await page.locator("button.ml4.css-xdirqf").click()
    await page.locator(
        f"p.fontWeight-b.f-shrink-0.css-i7pnfr:has-text('{index}')"
    ).wait_for(state="visible")

    return is_custom
