from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import quote, urlencode

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PWTimeoutError
from pydantic import BaseModel

from helpers import _close_page, require_env

BASE_URL = "https://app.ordermygear.com/"
ORDERS_URL = BASE_URL + "global/orders"
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
        return results

    row_count = await rows.count()

    for i in range(row_count):
        row = rows.nth(i)

        order_id_text = await row.locator("td:nth-child(2) strong").inner_text()
        store_name = await row.locator("td:nth-child(4)").inner_text()
        order_id = order_id_text.lstrip("#").strip()

        toggle = row.locator("td:last-child i.fa.fa-chevron-down")
        await toggle.scroll_into_view_if_needed()
        await toggle.click()

        expanded_tr = row.locator("xpath=following-sibling::tr[1]")
        await expanded_tr.wait_for(state="attached")

        await expanded_tr.locator("th:has-text('Product')").wait_for(state="visible")

        detail_table = expanded_tr.locator("table.css-1ago99h")
        product_rows = detail_table.locator("tr.css-98d6fm")
        pcount = await product_rows.count()
        items = []

        for j in range(pcount):
            pr = product_rows.nth(j)

            raw_name = await pr.locator(
                "td:nth-child(1) .css-1v85qd1 > strong"
            ).first.inner_text()
            name = raw_name.split(".", 1)[0].strip()

            color_text = await pr.locator(
                "td:nth-child(1) p:has-text('Color:')"
            ).first.inner_text()
            size_text = await pr.locator(
                "td:nth-child(1) p:has-text('Size:')"
            ).first.inner_text()
            color = color_text.split("Color:")[-1].strip()
            size = size_text.split("Size:")[-1].strip()

            quantity = (await pr.locator("td:nth-child(2)").inner_text()).strip()

            price_text = (await pr.locator("td:nth-child(4)").inner_text()).strip()
            total_text = (await pr.locator("td:nth-child(5)").inner_text()).strip()
            price = price_text.replace("$", "").strip()
            total = total_text.replace("$", "").strip()

            items.append(
                {
                    "name": name.strip(),
                    "color": color,
                    "size": size,
                    "quantity": quantity,
                    "price": price,
                    "total": total,
                }
            )

        results.append(
            {
                "id": order_id,
                "store_name": store_name.strip(),
                "order_name": store_name + " " + order_id,
                "items": items,
            }
        )
    await _close_page(page)
    return results
