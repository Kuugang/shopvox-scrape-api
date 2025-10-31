import re
from typing import Dict, List, Tuple

from dotenv import load_dotenv
from playwright.async_api import Locator, Page

from helpers import _click_and_wait_domcontent
from schemas import Item, SizeItem

load_dotenv()

URL_SANMAR = "https://sanmar.com"


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

        # pick first size label present in table
        size_key = next((c for c in candidates if c in size_entries), None)
        if not size_key:
            # no matching column → treat as OOS for this page
            oos_sizes.append(str(s.size))
            continue

        # size_entries[size_key] -> List[Tuple[warehouse_name: str, input: Locator, available_qty: int]]
        for wh_name, input_field, available_qty in size_entries[size_key]:
            if remaining <= 0:
                break

            # skip disabled/zero
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
                # if this cell fails, try next warehouse cell
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
