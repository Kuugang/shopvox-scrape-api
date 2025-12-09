import asyncio
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import quote, urlencode

from dotenv import load_dotenv
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PWTimeoutError
from pydantic import BaseModel

from helpers import _close_page, require_env

BASE_URL = "https://app.ordermygear.com/"
ORDERS_URL = BASE_URL + "global/orders"
load_dotenv()

OMG_EMAIL = require_env("OMG_EMAIL")
OMG_PASSWORD = require_env("OMG_PASSWORD")


class OrdersQuery(BaseModel):
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    sale_code: Optional[str] = None
    sales_rep: Optional[int] = None
    status_id: Optional[int] = None
    offset: int = 0
    size: int = 50


def build_orders_query(q: OrdersQuery) -> str:
    params: dict[str, str | int] = {
        "from": q.offset,
        "size": q.size,
    }

    if q.start_date is not None:
        params["startDate"] = q.start_date.strftime("%Y-%m-%d %H:%M:%S")
    if q.end_date is not None:
        params["endDate"] = q.end_date.strftime("%Y-%m-%d %H:%M:%S")

    if q.sale_code is not None:
        sale_code = q.sale_code
        if isinstance(sale_code, (list, tuple)):
            sale_code = ",".join(map(str, sale_code))
        params["saleCode"] = sale_code

    if q.sales_rep is not None:
        params["salesRep"] = q.sales_rep
    if q.status_id is not None:
        params["statusId"] = q.status_id

    query_string = urlencode(params, quote_via=quote, doseq=True)
    query_string = query_string.replace("%2C", ",")

    return f"{ORDERS_URL}?{query_string}"


async def login(page: Page):
    await page.goto(BASE_URL, wait_until="domcontentloaded")
    await page.wait_for_load_state("load")

    await page.get_by_role("textbox", name="Email address").fill(OMG_EMAIL)
    await page.get_by_role("textbox", name="Password").fill(OMG_PASSWORD)

    await page.get_by_role("button", name="Login", exact=True).click()

    await page.wait_for_timeout(10_000)
    await page.wait_for_load_state("load")
    await _close_page(page)


# ORDERS
# -----------------------------------------------------------------------------------------------------


async def update_orders(page: Page, q: OrdersQuery, payload: Dict[str, Any]):
    await page.goto(build_orders_query(q), wait_until="domcontentloaded")
    await page.wait_for_load_state("load")

    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_selector("tbody tr.css-3c5joz", state="attached")

    rows = page.locator("tbody tr.css-3c5joz")
    row_count = await rows.count()

    for i in range(row_count):
        row = rows.nth(i)

        toggle = row.locator("td:last-child i.fa.fa-chevron-down")
        await toggle.scroll_into_view_if_needed()
        await toggle.click()

        expanded_tr = row.locator("xpath=following-sibling::tr[1]")
        await expanded_tr.wait_for(state="attached")

        await expanded_tr.locator("th:has-text('Product')").wait_for(state="visible")

        # Status
        await page.locator("div.css-3z95z8").click()
        await page.locator(
            f"div.css-1vseq86 span:has-text('{payload.get("status")}')"
        ).click()

    await _close_page(page)


async def get_orders(page: Page, q: OrdersQuery):
    await page.goto(build_orders_query(q), wait_until="domcontentloaded")
    await page.wait_for_load_state("load")
    results = []

    rows = page.locator("tbody tr.css-3c5joz")
    try:
        await rows.first.wait_for(state="visible", timeout=5_000)
    except PWTimeoutError:
        await _close_page(page)
        return results

    row_count = await rows.count()

    MAX_RETRIES = 3
    RETRY_DELAY = 1

    for i in range(row_count):
        row = rows.nth(i)
        order_id_text = await row.locator("td:nth-child(2) strong").inner_text()
        store_name = await row.locator("td:nth-child(4)").inner_text()
        order_id = order_id_text.lstrip("#").strip()

        items = []
        is_expanded = False  # Track expansion state

        for retry in range(MAX_RETRIES):
            try:
                if not is_expanded:
                    toggle = row.locator("td:last-child i.fa.fa-chevron-down")
                    await toggle.scroll_into_view_if_needed()
                    await toggle.click()
                    is_expanded = True

                expanded_tr = row.locator("xpath=following-sibling::tr[1]")
                await expanded_tr.wait_for(state="attached", timeout=5000)
                await expanded_tr.locator("th:has-text('Product')").wait_for(
                    state="visible", timeout=5000
                )

                detail_table = expanded_tr.locator("table.css-1ago99h")
                product_rows = detail_table.locator("tr.css-98d6fm")
                pcount = await product_rows.count()

                if pcount == 0:
                    raise ValueError("No products found")

                for j in range(pcount):
                    pr = product_rows.nth(j)
                    raw_name = await pr.locator(
                        "td:nth-child(1) .css-1v85qd1 > strong"
                    ).first.inner_text()
                    if "." in raw_name:
                        # rsplit with maxsplit=1 splits from the right, only once
                        parts = raw_name.rsplit(".", 1)
                        name = parts[0].strip()
                        style = parts[1].strip()
                    else:
                        # No period, take last word as style
                        words = raw_name.strip().split()
                        if len(words) > 1:
                            name = " ".join(words[:-1])
                            style = words[-1]
                        else:
                            name = raw_name.strip()
                            style = ""

                    color_text = await pr.locator(
                        "td:nth-child(1) p:has-text('Color:')"
                    ).first.inner_text()
                    size_text = await pr.locator(
                        "td:nth-child(1) p:has-text('Size:')"
                    ).first.inner_text()
                    color = color_text.split("Color:")[-1].strip()
                    size = size_text.split("Size:")[-1].strip()
                    quantity = (
                        await pr.locator("td:nth-child(2)").inner_text()
                    ).strip()
                    price_text = (
                        await pr.locator("td:nth-child(4)").inner_text()
                    ).strip()
                    total_text = (
                        await pr.locator("td:nth-child(5)").inner_text()
                    ).strip()
                    price = price_text.replace("$", "").strip()
                    total = total_text.replace("$", "").strip()
                    items.append(
                        {
                            "name": name.strip(),
                            "color": color,
                            "style": style,
                            "size": size,
                            "quantity": quantity,
                            "price": price,
                            "total": total,
                        }
                    )

                if items:
                    break

            except (PWTimeoutError, ValueError, Exception) as e:
                if retry < MAX_RETRIES - 1:
                    print(
                        f"Retry {retry + 1}/{MAX_RETRIES} for order {order_id}: {str(e)}"
                    )
                    # Don't try to close it, just wait and retry
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    print(
                        f"Failed to extract items for order {order_id} after {MAX_RETRIES} retries"
                    )

        if not items:
            print(f"Warning: No items found for order {order_id}")
        seen = set()

        clean_store = store_name.strip()
        key = order_id

        if key in seen:
            pass
        else:
            seen.add(key)
            results.append(
                {
                    "id": order_id,
                    "store_name": clean_store,
                    "order_name": f"{clean_store} {order_id}",
                    "items": items,
                }
            )
    await _close_page(page)
    return results
