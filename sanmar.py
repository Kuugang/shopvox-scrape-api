import asyncio
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit, urlunsplit

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Frame, Locator, Page
from playwright.async_api import TimeoutError as PWTimeoutError
from playwright.async_api import expect

from helpers import (
    _as_list,
    _as_mapping,
    _clean,
    _click_and_wait_domcontent,
    _close_page,
    _safe_str,
    require_env,
)
from schemas import Item, SizeItem

load_dotenv()


SANMAR_USERNAME = require_env("SANMAR_USERNAME")
SANMAR_PASSWORD = require_env("SANMAR_PASSWORD")

URL_SANMAR = "https://sanmar.com/"
ACTIVE_ORDERS_URL = URL_SANMAR + "mysanmar/sales-orders/active-orders#"
ORDER_DETAILS_BASE = URL_SANMAR + "mysanmar/sales-orders/order-details"


UPS_SEL = {
    "tracking_number": "#stApp_trackingNumber",
    "delivered_label": "#st_App_DelvdLabel",
    "status_current": "#stApp_txtPackageStatus",
    "delivered_when": "#st_App_PkgStsMonthNum",
    "delivered_time": "#st_App_PkgStsTime",
    "delivered_loc": "#st_App_PkgStsLoc",
    "ship_to_city": "#stApp_txtAddress",
    "ship_to_country": "#stApp_txtCountry",
    "received_by": "#stApp_valReceivedBy",
}


async def login(page: Page):
    # page.on("popup", lambda p: asyncio.create_task(p.close()))
    await page.goto(URL_SANMAR, wait_until="domcontentloaded")
    await page.wait_for_load_state("load")

    await page.fill("#username", SANMAR_USERNAME)
    await page.fill("#password", SANMAR_PASSWORD)
    await page.locator("input.form-check-input").click()

    await page.locator(
        "button.btn-df.btn-primary-df.btn-sm-df.text-nowrap.d-none.d-lg-inline-block"
    ).click()

    await page.wait_for_load_state("networkidle")
    await _close_page(page)


async def process_item(page: Page, item: Item) -> Tuple[bool, List[str]]:
    await fill_search(page, item.part)
    await open_color_detail(page, item.color)
    return await add_requested_sizes(page, item.sizes)


async def home(page: Page):
    await page.goto(URL_SANMAR, wait_until="domcontentloaded")


async def build_size_inputs_by_warehouse(
    page: Page,
) -> Dict[str, List[Tuple[str, Locator, int]]]:
    await page.wait_for_selector(
        "table.table-inventory.table-inventory-next", timeout=15000
    )
    await page.wait_for_selector(
        "table.table-inventory.table-inventory-next thead th.size-header",
        timeout=15000,
    )

    size_to_entries: Dict[str, List[Tuple[str, Locator, int]]] = {}

    tables = page.locator("table.table-inventory.table-inventory-next")
    tcount = await tables.count()

    for t_idx in range(tcount):
        table = tables.nth(t_idx)

        # Scope all queries to this table only
        headers = table.locator(":scope thead th.size-header")
        rows = table.locator(":scope tr.default.warehouse-list")

        hcount = await headers.count()
        rcount = await rows.count()
        if hcount == 0 or rcount == 0:
            continue  # nothing to do on this table

        # Pre-read header labels to keep alignment with data-col-tracker
        header_labels: List[str] = []
        for h_idx in range(hcount):
            try:
                raw = (await headers.nth(h_idx).inner_text()).strip()
            except Exception:
                raw = ""
            header_labels.append(raw)

        # Walk rows (warehouses)
        for r_idx in range(rcount):
            row = rows.nth(r_idx)

            # Warehouse name (best-effort)
            wh_name = "Warehouse"
            try:
                wh_el = row.locator(":scope .warehouse-city").first
                if await wh_el.count() > 0:
                    wh_name = (await wh_el.inner_text() or "").strip() or wh_name
            except Exception:
                pass

            # For each size column, find the matching <td> and its input/stock
            for h_idx, raw_label in enumerate(header_labels):
                if not raw_label:
                    continue

                size_key = raw_label.strip().upper()

                td = row.locator(f":scope td[data-col-tracker='{h_idx}']")
                if await td.count() == 0:
                    continue

                input_field = td.locator(":scope input.form-control").first
                if await input_field.count() == 0:
                    # no input for this size/warehouse
                    continue

                # Read stock: prefer visible span, fallback to input data-available
                available_qty = 0
                try:
                    stock_span = td.locator(":scope span.stock-available").first
                    if await stock_span.count() > 0:
                        txt = (await stock_span.inner_text() or "").strip()
                        available_qty = int(re.sub(r"\D", "", txt) or "0")
                except Exception:
                    pass

                if available_qty == 0:
                    try:
                        data_avail = await input_field.get_attribute("data-available")
                        if data_avail is not None:
                            available_qty = int(re.sub(r"\D", "", data_avail) or "0")
                    except Exception:
                        pass

                # Record the entry regardless of availability (we'll decide later)
                size_to_entries.setdefault(size_key, []).append(
                    (wh_name, input_field, available_qty)
                )

    if len(size_to_entries) == 1:
        only_key = next(iter(size_to_entries.keys()))
        if only_key not in ("ONE SIZE", "OSFA"):
            size_to_entries["ONE SIZE"] = size_to_entries[only_key]
            size_to_entries["OSFA"] = size_to_entries[only_key]

    return size_to_entries


async def fill_search(page: Page, style_number: str):
    search_inputs = page.locator(
        'input#main-search[placeholder="Search by Product, Style Number, or Category"]'
    )
    scount = await search_inputs.count()
    if scount == 0:
        search_inputs = page.locator('input[aria-label*="Search"]')
        scount = await search_inputs.count()

    if scount == 0:
        raise RuntimeError("No search input found")

    target = search_inputs.first if scount == 1 else search_inputs.nth(1)
    await target.fill("")
    await target.fill(style_number)

    search_buttons = page.locator("button[aria-label='Search']")
    bcount = await search_buttons.count()

    if bcount == 1:
        await _click_and_wait_domcontent(page, search_buttons.first)
    else:
        await _click_and_wait_domcontent(page, search_buttons.nth(1))


async def open_color_detail(page: Page, color: str):
    wanted = " ".join(w.capitalize() for w in color.strip().split())
    color_a = page.locator(f"a[title='{wanted}']").first

    if await color_a.count() == 0:
        color_a = page.locator(f"a[title='{color}']").first
    if await color_a.count() == 0:
        color_a = page.locator("li.color-choices a", has_text=wanted).first
    if await color_a.count() == 0:
        color_a = page.locator("a", has_text=wanted).first

    if await color_a.count() == 0:
        raise RuntimeError(f"Color option not found: {color}")

    await color_a.wait_for(state="visible", timeout=7000)
    href = await color_a.get_attribute("href")
    if not href:
        await _click_and_wait_domcontent(page, color_a)
        return

    await page.goto(URL_SANMAR + href, wait_until="domcontentloaded")


async def add_requested_sizes(
    page: Page, sizes: List[SizeItem]
) -> Tuple[bool, List[str]]:
    try:
        size_entries = await build_size_inputs_by_warehouse(page)
    except Exception:
        await page.wait_for_timeout(800)
        size_entries = await build_size_inputs_by_warehouse(page)

    added_any = False
    oos_sizes: List[str] = []

    def normalize_size(label: str) -> List[str]:
        u = (label or "").strip().upper()
        variants = {u}
        alt = {
            "XS": {"XSM", "X-SMALL"},
            "S": {"SM", "SMALL"},
            "M": {"MED", "MEDIUM"},
            "L": {"LG", "LARGE"},
            "XL": {"X-LARGE", "XLG"},
            "2XL": {"XXL", "2X-LARGE"},
            "3XL": {"XXXL", "3X-LARGE"},
            "4XL": {"XXXXL", "4X-LARGE"},
            "5XL": {"XXXXXL", "5X-LARGE"},
            "6XL": {"XXXXXXL", "6X-LARGE"},
            "7XL": {"XXXXXXXL", "7X-LARGE"},
            "8XL": {"XXXXXXXXL", "8X-LARGE"},
            "9XL": {"XXXXXXXXXL", "9X-LARGE"},
            "ONE SIZE": {"OS", "OSFA"},
            "OSFA": {"ONE SIZE", "OS"},
        }
        for k, v in alt.items():
            if u == k or u in v:
                variants |= {k} | v
        return list(variants)

    for s in sizes:
        # sanitize
        if not s or s.quantity is None or int(s.quantity or 0) <= 0:
            continue

        target_qty = int(s.quantity)
        remaining = target_qty
        candidates = normalize_size(str(s.size or ""))

        size_key = next((c for c in candidates if c in size_entries), None)
        if not size_key:
            oos_sizes.append(str(s.size))
            continue

        for wh_name, input_field, available_qty in size_entries[size_key]:
            if remaining <= 0:
                break

            try:
                if await input_field.is_disabled():
                    continue
            except Exception:
                try:
                    if (await input_field.get_attribute("disabled")) is not None:
                        continue
                except Exception:
                    pass

            if available_qty <= 0:
                continue

            to_take = min(available_qty, remaining)
            try:
                await input_field.wait_for(state="visible", timeout=5000)
                await input_field.scroll_into_view_if_needed()
                await input_field.fill("")  # clear first
                await input_field.fill(str(to_take))  # then type
                added_any = True
                remaining -= to_take
            except Exception:
                continue

        if remaining > 0:
            oos_sizes.append(str(s.size))

    await page.wait_for_timeout(300)

    if added_any:
        add_to_cart_button = page.locator(
            "button.btn.btn-primary.btn-add-to-basket"
        ).first
        await add_to_cart_button.wait_for(state="visible", timeout=7000)
        await add_to_cart_button.click()
        await page.wait_for_timeout(500)

    return added_any, oos_sizes


async def get_active_order_status(page: Page):
    await page.goto(ACTIVE_ORDERS_URL)
    await page.get_by_role("link", name="Advanced Search").click()
    await page.get_by_label("Order Status").select_option("sanmarShipped")
    await page.locator('button[name="salesOrderSearchBtn"]').click()
    await page.wait_for_timeout(1000)
    await page.wait_for_selector(
        "#sales-order-table tbody tr.orders-separator", timeout=30000
    )
    results = await open_all_orders_in_parallel(
        page, max_concurrency=5, track_max_concurrency=2
    )

    print(f"[get_active_order_status] results: {results}")
    return results


def build_order_url(order_no: str) -> str:
    params = {
        "salesOrderNumber": order_no,
        "orderType": "blanks",
        "orderStatus": "sanmarShipped",
    }
    return f"{ORDER_DETAILS_BASE}?{urlencode(params)}"


def tracking_from_url(tracking_url: str) -> str | None:
    try:
        q = parse_qs(urlparse(tracking_url).query)
        val = (q.get("tracknum") or [None])[0]
        return val.split("/")[0] if val else None
    except Exception:
        return None


async def _extract_order_shipments(page: Page) -> dict[str, dict]:
    try:
        content = page.locator("#shipped-items-content")
        header = page.locator("#shipped-items-header")
        cls = await content.get_attribute("class") or ""
        if "show" not in cls and await header.count():
            await header.click()
            await content.wait_for(state="visible", timeout=5000)
    except Exception:
        pass

    shipments: dict[str, dict] = {}
    sections = page.locator("#shipped-items-content div.mt-3.hidden-xs")
    for i in range(await sections.count()):
        sec = sections.nth(i)

        # tracking anchor (header right side)
        track_a = sec.locator('.row.pt-2.pb-3 a[href*="track"]').first
        href = await track_a.get_attribute("href") if await track_a.count() else None
        tn = _clean(await track_a.inner_text()) if await track_a.count() else None

        # normalize tn from url if anchor text is weird
        tn = tn or (tracking_from_url(href or "") or None)
        if not tn:
            continue

        # rows with items: use the known row class for line rows
        # header row has 'fw-bold'; footer total row has an <h2>; we select typical line rows instead
        line_rows = sec.locator("table tbody tr.border-bottom-lite-gray.height-60")

        lines = []
        for r in range(await line_rows.count()):
            row = line_rows.nth(r)
            # skip footer total rows that include an <h2>
            if await row.locator("h2").count():
                continue

            tds = row.locator("td")
            if await tds.count() < 7:
                continue

            # columns (by observed structure):
            # [0] spacer, [1] style cell (contains <a> with style code), [2] Color, [3] Size,
            # [4] Quantity, [5] Price Per Item, [6] Total Amount
            style = _clean(await row.locator("td:nth-child(2) a").first.inner_text())
            color = _clean(await row.locator("td:nth-child(3)").first.inner_text())
            size = _clean(await row.locator("td:nth-child(4)").first.inner_text())
            qty_s = _clean(await row.locator("td:nth-child(5)").first.inner_text())
            ppi = _clean(await row.locator("td:nth-child(6)").first.inner_text())
            total = _clean(await row.locator("td:nth-child(7)").first.inner_text())

            # clean color to drop any swatch text preceding the color name
            # (often it's "<img ...> ColorName")
            if " " in color:
                # after image, color text is usually after a space
                color = color.split()[-len(color.split()) :]
                color = " ".join(color)
            # quantity to int if possible
            try:
                qty = int(qty_s.replace(",", ""))
            except Exception:
                qty = None

            lines.append(
                {
                    "style": style,  # Style #
                    "color": color,  # Color
                    "size": size,  # Size
                    "quantity": qty,  # Quantity
                    "price_each": ppi,  # Price Per Item
                    "line_total": total,  # Total Amount
                }
            )

        shipments[tn] = {"href": href, "lines": lines}

    return shipments


async def _find_ups_context(page: Page) -> Union[Page, Frame]:
    """
    Returns the context (Page or Frame) that actually contains UPS selectors.
    We first try the top page; if not found, we scan iframes.
    """
    sel = f'{UPS_SEL["tracking_number"]}, {UPS_SEL["status_current"]}'

    # 1) Try top-level page quickly
    try:
        await page.wait_for_selector(sel, timeout=2500)
        return page
    except PWTimeoutError:
        pass

    # 2) Search frames
    deadline = (
        page.context.timeouts()["default"]
        if hasattr(page.context, "timeouts")
        else None
    )
    end = page._loop.time() + (UPS_WAIT_TIMEOUT / 1000)

    while page._loop.time() < end:
        for fr in page.frames:
            try:
                # quick sanity: only check frames that have domcontentloaded
                await fr.wait_for_selector("html, body", timeout=500)
                # look for any UPS selector
                await fr.wait_for_selector(sel, timeout=1000)
                return fr
            except Exception:
                continue
        await asyncio.sleep(0.3)

    # final attempt: raise to caller
    raise PWTimeoutError(f"UPS context not found within {UPS_WAIT_TIMEOUT}ms")


async def _parse_ups_tracking(ctx: Union[Page, Frame]) -> dict:
    """
    Extract key tracking info from the UPS 'simplified tracking' page/frame.
    """

    async def _txt(sel: str) -> Optional[str]:
        loc = ctx.locator(sel).first
        try:
            if await loc.count() == 0:
                return None
            if not await loc.is_visible():
                return None
            return (await loc.inner_text()).strip()
        except Exception:
            return None

    tn = await _txt(UPS_SEL["tracking_number"])
    st = await _txt(UPS_SEL["status_current"])
    dwh = await _txt(UPS_SEL["delivered_when"])
    dti = await _txt(UPS_SEL["delivered_time"])
    dlo = await _txt(UPS_SEL["delivered_loc"])
    cty = await _txt(UPS_SEL["ship_to_city"])
    cty2 = await _txt(UPS_SEL["ship_to_country"])
    rcv = await _txt(UPS_SEL["received_by"])

    delivered = await ctx.locator(UPS_SEL["delivered_label"]).count() > 0

    return {
        "tracking_number": tn,
        "status": st,
        "delivered": delivered,
        "delivered_when_text": dwh,
        "delivered_time_text": dti,
        "delivered_location": dlo,
        "ship_to": " ".join([x for x in [cty, cty2] if x]),
        "received_by": rcv,
    }


UPS_WAIT_TIMEOUT = 60000
UPS_MAX_RETRIES = 3
UPS_BACKOFFS = [0.5, 1.5, 3.0]


def normalize_ups_url(href: str) -> str:
    parts = list(urlsplit(href))
    parts[2] = re.sub(r"/trackdetails/?$", "", parts[2])
    return urlunsplit(parts)


async def _dismiss_ups_banners(p: Page) -> None:
    try:
        btn = p.locator("#onetrust-accept-btn-handler")
        if await btn.count():
            await btn.click(timeout=3000)
    except Exception:
        pass
    try:
        alt = p.get_by_role("button", name=re.compile(r"Accept|Agree|Cookies", re.I))
        if await alt.count():
            await alt.first.click(timeout=3000)
    except Exception:
        pass
    try:
        alt_btn = p.get_by_role(
            "button", name=re.compile(r"Accept All Cookies|Agree|Accept", re.I)
        )
        if await alt_btn.count():
            await alt_btn.first.click(timeout=3000)
    except Exception:
        pass


async def _open_tracking_tab(context: BrowserContext, href: str, order_no: str):
    url = normalize_ups_url(href)
    p = await context.new_page()
    try:
        for attempt in range(UPS_MAX_RETRIES):
            try:
                await p.goto(url, wait_until="domcontentloaded")
                await p.wait_for_load_state("load")
                await _dismiss_ups_banners(p)

                ctx = await _find_ups_context(p)
                details = await _parse_ups_tracking(ctx)
                return {
                    "order": order_no,
                    "tracking_url": url,
                    "details": details,
                }
            except Exception:
                if attempt < UPS_MAX_RETRIES - 1:
                    await asyncio.sleep(
                        UPS_BACKOFFS[min(attempt, len(UPS_BACKOFFS) - 1)]
                    )
                    continue
    finally:
        try:
            await _close_page(p)
        except Exception:
            pass


async def _open_all_tracking_for_order(
    page: Page,
    context: BrowserContext,
    order_no: str,
    track_max_concurrency: int | None = None,
):
    shipments_by_tn = await _extract_order_shipments(page)

    tracking_links = page.locator('#shipped-items-content a[href*="track"]')
    try:
        header = page.locator("#shipped-items-header")
        if await header.is_visible():
            content = page.locator("#shipped-items-content")
            if not await content.get_attribute("class") or "show" not in (
                await content.get_attribute("class") or ""
            ):
                await header.click()
                await expect(content).to_be_visible(timeout=5000)
    except Exception:
        pass

    count = await tracking_links.count()
    if count == 0:
        return []

    hrefs = set()
    for i in range(count):
        href = await tracking_links.nth(i).get_attribute("href")
        if href:
            hrefs.add(href)

    # open all UPS tabs in parallel (with optional semaphore)
    if track_max_concurrency and track_max_concurrency > 0:
        sem = asyncio.Semaphore(track_max_concurrency)

        async def sem_task(u: str):
            async with sem:
                return await _open_tracking_tab(context, u, order_no)

        tasks = [asyncio.create_task(sem_task(u)) for u in hrefs]
    else:
        tasks = [
            asyncio.create_task(_open_tracking_tab(context, u, order_no)) for u in hrefs
        ]

    results = await asyncio.gather(*tasks)

    enriched: List[Dict[str, Any]] = []

    for r in results or []:
        r_map: Mapping[str, Any] = _as_mapping(r)
        details: Mapping[str, Any] = _as_mapping(r_map.get("details"))

        tracking_url = _safe_str(r_map.get("tracking_url"))
        tn = (
            _safe_str(details.get("tracking_number"))
            or tracking_from_url(tracking_url)
            or ""
        )

        items = _as_mapping(shipments_by_tn.get(tn, {})).get("lines")
        items_list: List[Mapping[str, Any]] = _as_list(items)

        enriched.append(
            {
                "order": r_map.get("order"),
                "tracking_url": tracking_url or None,
                "details": dict(details),
                "items": items_list,
            }
        )

    return enriched


async def _open_order_in_new_tab(
    context: BrowserContext,
    so_no: str,
    po_no: str,
    track_max_concurrency: int | None = None,
):
    p = await context.new_page()
    url = build_order_url(so_no)
    try:

        await p.goto(url, wait_until="domcontentloaded")
        await p.wait_for_url(lambda u: f"salesOrderNumber={so_no}" in u, timeout=30000)
        await p.wait_for_load_state("networkidle")

        tracking_results = await _open_all_tracking_for_order(
            p, context, so_no, track_max_concurrency=track_max_concurrency
        )

        return {
            "order": so_no,
            "po": po_no,
            "tracking_results": tracking_results,
        }
    except Exception as e:
        return {"order": so_no, "status": f"failed: {e}", "tracking_results": []}
    finally:
        # Close the order-details tab
        try:
            await _close_page(p)
        except Exception:
            pass


async def open_all_orders_in_parallel(
    page: Page,
    max_concurrency: int | None = None,
    track_max_concurrency: int | None = None,
):
    order_strongs = page.locator("#sales-order-table td.col-order-number a strong")
    po_numbers_span = page.locator("#sales-order-table td.col-purchase-order span")

    count = await order_strongs.count()
    if count == 0:
        return []

    pairs: list[tuple[str, str]] = []
    po_count = await po_numbers_span.count()

    for i in range(count):
        so_txt = (await order_strongs.nth(i).inner_text()).strip()
        if not so_txt:
            continue

        po_txt = ""
        if i < po_count:
            raw = (await po_numbers_span.nth(i).inner_text()) or ""
            po_txt = raw.strip() or ""

        pairs.append((so_txt, po_txt))

    context = page.context

    async def _task(so_no: str, po_no: str):
        return await _open_order_in_new_tab(
            context,
            so_no,
            po_no,
            track_max_concurrency=track_max_concurrency,
        )

    if max_concurrency and max_concurrency > 0:
        sem = asyncio.Semaphore(max_concurrency)

        async def sem_task(so_no: str, po_no: str):
            async with sem:
                return await _task(so_no, po_no)

        tasks = [
            asyncio.create_task(sem_task(so_no, po_no)) for (so_no, po_no) in pairs
        ]
    else:
        tasks = [asyncio.create_task(_task(so_no, po_no)) for (so_no, po_no) in pairs]

    return await asyncio.gather(*tasks)
