import asyncio
import datetime
import os
import re
import tempfile
from contextlib import asynccontextmanager
from typing import (Any, AsyncIterator, Awaitable, Callable, Dict, List,
                    Optional, Tuple, Union)

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from playwright.async_api import BrowserContext
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Playwright
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

import s_and_s
import sanmar
from helpers import require_env
from schemas import Item, JobFilters, JobFiltersModel, MfaBodyModel, SalesOrder

load_dotenv()

URL_SANMAR = "https://sanmar.com"
URL_SHOPVOX = "https://express.shopvox.com"

SHOPVOX_EMAIL = require_env("SHOPVOX_EMAIL")
SHOPVOX_PASSWORD = require_env("SHOPVOX_PASSWORD")

SANMAR_USERNAME = require_env("SANMAR_USERNAME")
SANMAR_PASSWORD = require_env("SANMAR_PASSWORD")

USER_DATA_DIR = require_env("PW_USER_DATA_DIR")
HEADLESS = require_env("PW_HEADLESS").lower() != "false"

_pw: Playwright | None = None
_ctx: BrowserContext | None = None
_lock = asyncio.Lock()


SALES_REP_LINKS: Dict[str, str] = {
    "colby": "jobs?view=b36878a1-bdda-4eed-94ab-e42b60ac7e15",
    "courtney": "jobs?view=d2f04e58-5605-43ef-997c-4bc2b78db50f",
}

def _require_creds():
    if not SHOPVOX_EMAIL or not SHOPVOX_PASSWORD:
        raise HTTPException(
            status_code=400,
            detail="Missing SHOPVOX_EMAIL or SHOPVOX_PASSWORD in environment (.env)",
        )


async def _safe_inner_text(locator, timeout: int = 250) -> Optional[str]:
    try:
        return (await locator.inner_text(timeout=timeout)).strip()
    except Exception:
        return None


def _parse_part_code(product_line_text: Optional[str]) -> Optional[str]:
    if not product_line_text:
        return None
    if " - " in product_line_text:
        tail = product_line_text.split(" - ")[-1].strip()
        return re.sub(r"[^\w\-]+$", "", tail)
    m = re.findall(r"[A-Za-z0-9\-]+", product_line_text)
    return m[-1] if m else product_line_text.strip()


def _normalize_size_label(label: str) -> str:
    if not label:
        return "qty"
    u = label.strip().upper()
    canonical = {
        "XSM": "XS", "X-SMALL": "XS",
        "SM": "S", "SMALL": "S",
        "MED": "M", "MEDIUM": "M",
        "LG": "L", "LARGE": "L",
        "XLG": "XL", "X-LARGE": "XL",
        "XXL": "2XL", "2X-LARGE": "2XL",
        "XXXL": "3XL", "3X-LARGE": "3XL",
        "XXXXL": "4XL", "4X-LARGE": "4XL",
        # one-size variants:
        "OS": "ONE SIZE",
        "OSFA": "ONE SIZE",
        "ONE SIZE FITS ALL": "ONE SIZE",
        "QTY": "qty",
    }
    return canonical.get(u, u)
def _normalize_key_text(s: str) -> str:
    return (s or "").strip()

def _to_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    ss = s.strip()
    if not ss:
        return None
    ss = ss.replace(",", "")
    ss = re.sub(r"[^\d.\-]", "", ss)
    try:
        return float(ss)
    except ValueError:
        return None


async def _safe_input_value(locator, timeout: int = 250) -> Optional[str]:
    try:
        return (await locator.input_value(timeout=timeout)).strip()
    except Exception:
        return None

def _normalize_store(s: Optional[str]) -> str:
    return (s or "").strip().casefold()



def safe_remove(path: str):
    """Safely remove temp file if it exists."""
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"Cleanup failed for {path}: {e}")


async def _init_playwright_and_context():
    """
    Internal: start Playwright and create a persistent context exactly once.
    """
    global _pw, _ctx
    async with _lock:
        if _pw is None:
            _pw = await async_playwright().start()
        if _ctx is None:
            _ctx = await _pw.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                headless=HEADLESS,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )


async def _shutdown_playwright():
    global _pw, _ctx
    if _ctx is not None:
        try:
            await _ctx.close()
        except Exception:
            pass
        _ctx = None
    if _pw is not None:
        try:
            await _pw.stop()
        except Exception:
            pass
        _pw = None


async def get_ctx() -> BrowserContext:
    """
    Public helper: returns the long-lived persistent context.
    Ensures it exists if called very early.
    """
    if _ctx is None:
        await _init_playwright_and_context()
    assert _ctx is not None
    return _ctx


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await _init_playwright_and_context()
    try:
        yield
    finally:
        await _shutdown_playwright()




async def clean_not_order_yet_tags(
    page: Page,
    orders: List[str],
    max_concurrency: int = 4,
    goto_timeout_ms: int = 45_000,
):

    ctx = await get_ctx()
    BADGE_TEXT = "NOT ORDER YET"

    async def pick_ordered_and_submit(p: Page) -> None:
        modal = p.locator("#root-modals-dropdowns [role='dialog']").first
        await modal.wait_for(state="visible", timeout=10_000)

        indicator = modal.locator(".css-1xb41ip-indicatorContainer, [class*='indicatorContainer']").last
        if await indicator.count() > 0:
            await indicator.click()
        else:
            combo = modal.get_by_role("combobox").first
            if await combo.count() > 0:
                await combo.click()

        listbox = p.locator(
            "#react-select-2-listbox._options_y8hy2_13.intercom-target-select-field-options.css-uvrstl[role='listbox']"
        )
        if await listbox.count() == 0:
            listbox = p.locator("[role='listbox'][id^='react-select-']")

        await listbox.wait_for(state="attached", timeout=10_000)

        try:
            await listbox.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            await listbox.evaluate("""
            el => {
              const getStyle = n => n && n.ownerDocument.defaultView.getComputedStyle(n);
              let p = el.parentElement;
              while (p) {
                const s = getStyle(p);
                if (s && (s.overflowY === 'auto' || s.overflowY === 'scroll')) {
                  p.scrollTop = 0;
                  p.scrollIntoView({ block: 'center' });
                  break;
                }
                p = p.parentElement;
              }
              el.scrollIntoView({ block: 'center' });
            }
            """)
        except Exception:
            pass

        submit_btn = modal.locator("button.ml4.css-12lhddq").first
        if await submit_btn.count() == 0:
            submit_btn = modal.locator("button[type='submit']").first
        await submit_btn.wait_for(state="visible", timeout=10_000)
        await submit_btn.click()
        await page.wait_for_timeout(5_000)

    async def tag_cleanup_on_order_page(p: Page) -> None:
        await page.wait_for_timeout(2000)
        badge = p.locator(f"span:has-text('{BADGE_TEXT}')").first
        await badge.wait_for(state="visible", timeout=10_000)
        await badge.click()

        modal = p.locator("#root-modals-dropdowns [role='dialog']").first
        await modal.wait_for(state="visible", timeout=10_000)

        remove_btn = modal.locator(
            f".css-1rdcdvo-multiValue:has-text('{BADGE_TEXT}') [role='button'][aria-label^='Remove']"
        ).first
        if await remove_btn.count() == 0:
            remove_btn = modal.locator(
                f":is(div, span):has-text('{BADGE_TEXT}') >> [role='button'][aria-label^='Remove']"
            ).first
        if await remove_btn.count() == 0:
            remove_btn = modal.locator("[role='button'][aria-label^='Remove']").first
        if await remove_btn.count() > 0:
            await remove_btn.click()

        indicator = modal.locator(".css-1xb41ip-indicatorContainer").last
        if await indicator.count() == 0:
            indicator = modal.locator("[class*='indicatorContainer']").last
        if await indicator.count() > 0:
            await indicator.click()
        else:
            combo = modal.locator("[role='combobox']").first
            if await combo.count() > 0:
                await combo.click()

        await pick_ordered_and_submit(p)


    async def run_one(idx: int, order: str, sem: asyncio.Semaphore):
        async with sem:
            page = await ctx.new_page()
            page.on("popup", lambda p: asyncio.create_task(p.close()))
            await page.goto(order, wait_until="domcontentloaded", timeout=goto_timeout_ms)
            await page.wait_for_load_state("load")
            await tag_cleanup_on_order_page(page)

            await page.close()

    sem = asyncio.Semaphore(max_concurrency)
    tasks = [asyncio.create_task(run_one(i, o, sem)) for i, o in enumerate(orders)]
    await asyncio.gather(*tasks, return_exceptions=False)


async def add_to_cart(orders: List["SalesOrder"], max_concurrency: int = 3):
    STORE_HOMES: Dict[str, Callable[[Page], Awaitable[None]]] = {
        "sanmar": sanmar.home,
        "s&s activewear": s_and_s.home,
    }

    STORE_PROCESSORS: Dict[str, Callable[[Page, Item], Awaitable[Tuple[bool, List[str]]]]] = {
        "sanmar": sanmar.process_item,
        "s&s activewear":s_and_s.process_item,
    }

    ctx = await get_ctx()

    async def process_order(order: SalesOrder, sem: asyncio.Semaphore, ctx) -> Dict:
        order.items.sort(key=lambda it: (it.store).lower(), reverse=True)

        page = await ctx.new_page()
        page.on("popup", lambda p: asyncio.create_task(p.close()))

        all_out_of_stock: Dict[str, List[str]] = {}
        skipped_custom: List[Dict[str, str]] = []
        processed_items: List[Dict[str, str]] = []
        any_added_overall = False

        by_store: Dict[str, List[Item]] = {}
        for it in order.items:
            by_store.setdefault(_normalize_store(it.store), []).append(it)

        sanmar_key = "sanmar"
        s_and_s_key = "s&s activewear"

        sanmar_items = by_store.get(sanmar_key, [])
        s_and_s_items = by_store.get(s_and_s_key, [])

        other_items = [it for k, group in by_store.items() if k != sanmar_key for it in group]

        async with sem:
            try:
                for store_key, group in by_store.items():
                    processor = STORE_PROCESSORS.get(store_key)
                    home = STORE_HOMES.get(store_key)

                    if home:
                        await home(page)

                    if processor:
                        for it in group:
                            await processor(page, it)
                            processed_items.append({"part": it.part, "color": it.color })

                for item in other_items:
                    skipped_custom.append(
                        {
                            "part": item.part,
                            "color": item.color,
                            "store": item.store,
                        }
                    )

            finally:
                if page:
                    await page.close()

        has_oos = bool(all_out_of_stock)
        has_custom = bool(skipped_custom)
        processed_sanmar = bool(processed_items)

        if not processed_sanmar and has_custom:
            status = "custom_store_only"
            base_msg = f"Order contains only non-SanMar items; none processed. Skipped: {skipped_custom}"
        elif processed_sanmar and not any_added_overall and has_oos:
            status = "out_of_stock"
            base_msg = f"All requested sizes for SanMar items are out of stock: {all_out_of_stock}"
        elif has_oos and any_added_overall:
            status = "partial"
            base_msg = f"Some items were out of stock: {all_out_of_stock}"
        elif any_added_overall:
            status = "success"
            base_msg = "All items added successfully"
        else:
            status = "no_items_added"
            base_msg = "No items were added to cart."

        return {
            "order_id": order.id,
            "url": order.url,
            "customer": order.customer,
            "status": status,
            "message": base_msg,
            "details": {
                "out_of_stock": all_out_of_stock,
                "skipped_custom": skipped_custom,
                "processed": processed_items,
                "any_added_overall": any_added_overall,
            },
        }

    sem = asyncio.Semaphore(max_concurrency)
    tasks = [asyncio.create_task(process_order(order, sem, ctx)) for order in orders]
    return await asyncio.gather(*tasks, return_exceptions=False)

    
    async def process_order(order: "SalesOrder", sem: asyncio.Semaphore):
        def is_sanmar(store: str) -> bool:
            return (store or "").strip().lower() == "sanmar"

        async with sem:
            has_sanmar = any(((it.store).strip().casefold() == "sanmar") for it in order.items)

            page = await ctx.new_page()
            page.on("popup", lambda p: asyncio.create_task(p.close()))
            if has_sanmar:
                await goto_home(page)

            all_out_of_stock: Dict[str, List[str]] = {}     # {part: [sizes]}
            skipped_custom: List[Dict[str, str]] = []       # [{part,color,store}]
            processed_items: List[Dict[str, str]] = []      # [{part,color}]
            any_added_overall = False

            for item in order.items:
                if item.store == "sanmar":
                    await process_sanmar(item)

                if item.store == "sanmar":
                    await process_s_and_s(item)

                if not is_sanmar(item.store):
                    skipped_custom.append(
                        {
                            "part": getattr(item, "part", ""),
                            "color": getattr(item, "color", ""),
                            "store": getattr(item, "store", ""),
                        }
                    )
                    continue

                await fill_search(page, item.part)
                await open_color_detail(page, item.color)
                oos_sizes, added_any = await add_requested_sizes(page, item.sizes)

                if oos_sizes:
                    all_out_of_stock[item.part] = oos_sizes
                if added_any:
                    any_added_overall = True
                processed_items.append({"part": item.part, "color": item.color})

            await page.close()

            has_oos = bool(all_out_of_stock)
            has_custom = bool(skipped_custom)
            processed_sanmar = bool(processed_items)

            # Decide status
            if not processed_sanmar and has_custom:
                status = "custom_store_only"
                base_msg = f"Order contains only non-SanMar items; none processed. Skipped: {skipped_custom}"
            elif processed_sanmar and not any_added_overall and has_oos:
                status = "out_of_stock"
                base_msg = f"All requested sizes for SanMar items are out of stock: {all_out_of_stock}"
            elif has_oos and any_added_overall:
                status = "partial"
                base_msg = f"Some items were out of stock: {all_out_of_stock}"
            elif any_added_overall:
                status = "success"
                base_msg = "All items added successfully"
            else:
                # Fallback: processed but neither added nor flagged OOS (e.g., mismatched sizes)
                status = "no_items_added"
                base_msg = "No items were added to cart."

            return {
                "order_id": order.id,
                "url": order.url,
                "customer": order.customer,
                "status": status,
                "message": base_msg,
                "details": {
                    "out_of_stock": all_out_of_stock,
                    "skipped_custom": skipped_custom,
                    "processed": processed_items,
                    "any_added_overall": any_added_overall,
                },
            }




async def get_sales_orders_urls(page: Page):
    sos: List[Dict[str, Any]] = []

    content = page.locator("div._contentWrapper_12otk_183")
    try:
        # Wait up to 30 seconds for the element to appear
        await content.wait_for(state="visible", timeout=30_000)
    except PWTimeout:
        # If the element never appears, return an empty list
        return sos

    rows = content.locator("div._rowWrapper_12otk_135.position-r")

    await rows.first.wait_for(state="visible", timeout=15_000)

    seen = -1
    stable = 0
    while True:
        count = await rows.count()
        if count == seen:
            stable += 1
        else:
            stable = 0
            seen = count
        if stable >= 2:
            break
        try:
            await content.evaluate("el => el.scrollBy(0, el.scrollHeight)")
        except Exception:
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        await page.wait_for_timeout(200)

    total_rows = await rows.count()

    for i in range(total_rows):
        row = rows.nth(i)
        link = row.locator(
            "div[header='SO#'] a._primaryLink_18702_1[href^='/transactions/sales-orders/']"
        ).first
        href = await link.get_attribute("href")

        id = await row.locator("a._primaryLink_18702_1.py4.px8").inner_text()

        customer_el = row.locator(
            "div[header='Customer'] a[href^='/customers/'] div.ml4"
        ).first

        customer = (await customer_el.inner_text()).strip()


        if href:
            sos.append(
                {
                    "id": int(id),
                    "href": href,
                    "customer": customer 
                }
            )
    return sos


async def extract_line_items(page) -> List[Dict[str, Any]]:
    line_items: List[Dict[str, Any]] = []

    # 1) Find the items area (virtualized container) or fallbacks
    items_container = page.locator("[class^='_lineItemPreview_']").first
    if await items_container.count() == 0:
        items_container = page.locator("[class^='_lineItemPreviewParameters_']").first
    if await items_container.count() == 0:
        items_container = page.locator("main, div._contentWrapper_12otk_183").first

    # Wait for visibility but don't explode if it never appears
    try:
        await items_container.wait_for(state="visible", timeout=10_000)
    except Exception:
        # Proceed anyway; some pages may render cards outside the expected wrapper
        pass

    # 2) Force attach virtualized rows by scrolling
    for _ in range(8):
        # If we already see any size rows or any line preview names, we're good
        try:
            if (
                await page.locator(
                    ":is(.PricingTemplateApparelItemsItemSizesSize, [class^='_lineItemPreviewName_'])"
                ).count()
                > 0
            ):
                break
        except Exception:
            pass

        try:
            # Prefer scrolling the container (virtual lists)
            if await items_container.count() > 0:
                await items_container.evaluate("el => { el.scrollTop = el.scrollHeight }")
            else:
                raise RuntimeError("no container")
        except Exception:
            # Fallback: scroll window
            try:
                await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            except Exception:
                pass
        await page.wait_for_timeout(200)

    # 3) CARD selection: prefer apparel description cards; fallback to generic white cards
    cards = page.locator("div.bg-white:has([class*='_apparelItemPricingDescriptionItemName_'])")
    if await cards.count() == 0:
        cards = page.locator("div.bg-white.borderRadius-8.p8")
    if await cards.count() == 0:
        cards = page.locator("div.bg-white")

    ccount = await cards.count()
    for i in range(ccount):
        card = cards.nth(i)

        # Apparel layout?
        desc_block = card.locator("[class*='_apparelItemPricingDescriptionItemName_']").first
        is_apparel = (await desc_block.count()) > 0

        store = name_text = color = part = ""

        if is_apparel:
            store_p = desc_block.locator("p.css-i7pnfr:not(.mt4)").first
            name_p  = desc_block.locator("p.mt4.css-i7pnfr").first
            color_p = desc_block.locator("p.css-ifbqr7").first

            name_text = (await _safe_inner_text(name_p)) or ""
            color     = (await _safe_inner_text(color_p)) or ""
            store     = (await _safe_inner_text(store_p)) or ""
            part      = _parse_part_code(name_text) or ""
        else:
            # Generic line item preview
            name_p = card.locator("[class^='_lineItemPreviewName_'] p.css-i7pnfr").first
            name_text = (await _safe_inner_text(name_p)) or ""
            part = _parse_part_code(name_text) or ""
            store = (await _safe_inner_text(card.locator("p.css-i7pnfr:not(.mt4)").first)) or "Custom"
            color = (await _safe_inner_text(card.locator("p.css-ifbqr7").first)) or ""

        # SIZE rows (apparel) or single quantity input (non-apparel)
        size_rows_loc = card.locator(
            "div._apparelItemSizesPricing_tgx96_24 > div.PricingTemplateApparelItemsItemSizesSize"
        )

        sizes_list: List[Dict[str, Any]] = []
        total_qty_for_card = 0.0
        rcount = await size_rows_loc.count()

        if rcount == 0:
            # Non-apparel: one quantity input
            qty_input = card.locator(
                "input[name*='.quantity'], input#quantity-input, input[name='quantity']"
            ).first
            qty_val_opt = await _safe_input_value(qty_input)
            qty_val = _to_float(qty_val_opt) or 0.0
            if qty_val > 0:
                sizes_list.append({"size": "qty", "quantity": float(qty_val)})
                total_qty_for_card += float(qty_val)
        else:
            for j in range(rcount):
                size_row = size_rows_loc.nth(j)
                size_label = (
                    await _safe_inner_text(size_row.locator("div._apparelItemSizesPricingLabel_tgx96_30").first)
                ) or ""
                qty_val_opt = await _safe_input_value(size_row.locator("input[type='text']").first)
                qty_val = _to_float(qty_val_opt) or 0.0
                sizes_list.append({"size": size_label, "quantity": float(qty_val)})
                total_qty_for_card += float(qty_val)

        if not sizes_list:
            # nothing to emit for this card
            continue

        for s in sizes_list:
            line_items.append(
                {
                    "name": name_text,
                    "part": part,
                    "color": color,
                    "store": store or "Custom",
                    "size": s["size"],
                    "quantity": float(s["quantity"]),
                }
            )

    # 4) Merge by (part, color, store) and SUM per-size quantities (dedupe sizes)
    merged: Dict[tuple, Dict[str, Any]] = {}

    for item in line_items:
        part_key  = _normalize_key_text(item.get("part", ""))
        color_key = _normalize_key_text(item.get("color", ""))
        store_key = _normalize_key_text(item.get("store", "Custom"))

        key = (part_key, color_key, store_key)
        if key not in merged:
            merged[key] = {
                "name": item.get("name", ""),
                "part": part_key,
                "color": color_key,
                "store": store_key or "Custom",
                "sizes": [],          # filled after summation
                "_sizes_map": {},     # internal: size -> qty
                "total_quantity": 0.0,
            }

        bucket = merged[key]
        size_label = _normalize_size_label(item.get("size", "") or "")
        qty = float(item.get("quantity") or 0.0)

        prev = float(bucket["_sizes_map"].get(size_label, 0.0))
        bucket["_sizes_map"][size_label] = prev + qty
        bucket["total_quantity"] = float(bucket["total_quantity"]) + qty

    # 5) Finalize output (convert _sizes_map to sorted list)
    def _size_sort_key(s: Dict[str, Any]) -> int:
        order = {
            "XS": 1, "S": 2, "M": 3, "L": 4, "XL": 5, "2XL": 6, "3XL": 7, "4XL": 8,
            "ONE SIZE": 100, "qty": 999,
        }
        return order.get(s["size"], 50)

    result: List[Dict[str, Any]] = []
    for v in merged.values():
        v["total_quantity"] = float(round(float(v["total_quantity"]), 2))

        sizes: List[Dict[str, Any]] = []
        for sz, q in v["_sizes_map"].items():
            sizes.append({"size": sz, "quantity": float(round(float(q), 2))})
        sizes.sort(key=_size_sort_key)

        v["sizes"] = sizes
        v.pop("_sizes_map", None)
        result.append(v)

    return result

async def get_so_details_parallel(
    page: Page,
    sos: List[dict],
    max_concurrency: int = 8,
    wait_ms_between_starts: int = 100,
) -> List[Dict[str, Any]]:
    total = len(sos)

    results: List[Dict[str, Any]] = [
        {"url": u["href"], "id": u["id"], "items": []}
        for u in sos
        if u.get("href") and u.get("id")
    ]

    if total == 0:
        return results

    ctx = page.context
    sem = asyncio.Semaphore(max_concurrency)

    async def fetch_one(idx: int, so: dict):
        full_url = URL_SHOPVOX + so["href"]
        async with sem:
            if wait_ms_between_starts > 0:
                await asyncio.sleep(wait_ms_between_starts / 1000)

            p = await ctx.new_page()
            items = []

            for _ in range(4):
                await p.goto(full_url, wait_until="domcontentloaded")
                await p.wait_for_selector("h2.css-ycj89q:has-text('Items')", timeout=15_000)
                await page.wait_for_timeout(5000)

                items = await extract_line_items(p)

                if len(items) > 0:
                    break


            results[idx] = {
                "url": full_url,
                "id": so["id"],
                "customer": so['customer'],
                "items": items,
                "total": sum(i.get("total_quantity", 0) or 0 for i in items),
            }
            await p.close()

    tasks = [asyncio.create_task(fetch_one(i, so)) for i, so in enumerate(sos)]
    await asyncio.gather(*tasks, return_exceptions=False)
    return results


async def fetch_overdue_jobs() -> Union[str, dict]:
    """
    Navigates to Jobs view and downloads the exported PDF.
    Returns:
      - str: path to saved PDF on success
      - dict: {"error": "..."} on failure
    """

    ctx = await get_ctx()
    page = await ctx.new_page()
    page.on("popup", lambda p: asyncio.create_task(p.close()))

    try:

        await page.goto(URL_SHOPVOX + "/jobs?view=f60b58c5-eb32-461b-9fed-05d6ac6d9ce3")
        await page.locator("span:has-text('Jobs')").wait_for(state="visible")
        await page.wait_for_timeout(10000)

        rows_count_text = await page.locator("p.css-ifbqr7").inner_text()
        m = re.search(r"(\d[\d,]*)", rows_count_text)
        rows_count = int(m.group(1).replace(",", "")) if m else None

        if rows_count == 0:
            await page.close()
            return ""

        await page.locator("button.css-obi7n2").click()
        await page.locator("div.display-b.textDecoration-n.cursor-p.text-black").nth(
            1
        ).click()

        async with page.expect_download() as download_info:
            await page.locator("button.css-xdirqf").click()
        download = await download_info.value

        tmp_dir = tempfile.gettempdir()
        pdf_path = os.path.join(tmp_dir, download.suggested_filename)
        await download.save_as(pdf_path)
        await page.close()

        return pdf_path

    except PlaywrightError as e:
        await page.close()

        return {"error": f"Playwright error: {str(e)}"}
    except Exception as e:
        await page.close()

        return {"error": f"Unexpected error: {str(e)}"}


async def fetch_pending_jobs(filters: JobFilters) -> Union[str, dict]:
    ctx = await get_ctx()
    page = await ctx.new_page()
    page.on("popup", lambda p: asyncio.create_task(p.close()))

    try:

        sales_rep = filters.get("sales_rep")
        rep_link = None

        if sales_rep:
            key = sales_rep.strip().lower()
            rep_link = SALES_REP_LINKS.get(key)
            if rep_link is None:
                return {
                    "error": f"Unknown sales_rep '{sales_rep}'. "
                    f"Allowed: {', '.join(SALES_REP_LINKS.keys())}"
                }

            await page.goto(URL_SHOPVOX + "/" + rep_link)
        await page.locator("span:has-text('Jobs')").wait_for(state="visible")
        await page.wait_for_timeout(10000)

        rows_count_text = await page.locator("p.css-ifbqr7").inner_text()
        m = re.search(r"(\d[\d,]*)", rows_count_text)
        rows_count = int(m.group(1).replace(",", "")) if m else None

        if rows_count == 0:
            await page.close()
            return ""

        await page.locator("button.css-obi7n2").click()
        await page.locator("div.display-b.textDecoration-n.cursor-p.text-black").nth(
            1
        ).click()

        async with page.expect_download() as download_info:
            await page.locator("button.css-xdirqf").click()
        download = await download_info.value

        tmp_dir = tempfile.gettempdir()
        pdf_path = os.path.join(tmp_dir, download.suggested_filename)
        await download.save_as(pdf_path)
        await page.close()

        return pdf_path

    except PlaywrightError as e:
        await page.close()

        return {"error": f"Playwright error: {str(e)}"}
    except Exception as e:
        await page.close()

        return {"error": f"Unexpected error: {str(e)}"}


async def fetch_to_order_so():
    ctx = await get_ctx()
    page = await ctx.new_page()
    page.on("popup", lambda p: asyncio.create_task(p.close()))

    try:
        await page.goto(
            URL_SHOPVOX
            + "/transactions/sales-orders?view=2225c6de-1500-414d-b393-1d0a5b098fef"
        )
        await page.locator("span:has-text('Sales Orders')").wait_for(state="visible")
        await page.wait_for_timeout(5000)
        so_urls_full = await get_sales_orders_urls(page)

        so_urls = [
            {"href": u["href"], "id": u["id"], "customer": u['customer']}
            for u in so_urls_full
            if u.get("href") and u.get("id")
        ]

        result = await get_so_details_parallel(page, so_urls)
        await page.close()
        return result

    except PlaywrightError as e:
        await page.close()

        return {"error": f"Playwright error: {str(e)}"}
    except Exception as e:
        await page.close()

        return {"error": f"Unexpected error: {str(e)}"}


app = FastAPI(title="ShopVox Scrape API", version="1", lifespan=lifespan)


@app.get("/login/shopvox")
async def login():
    """
    Fill creds and click Sign In.
    Returns:
      - 202 + {status:"mfa_required"} if OTP UI is shown
      - 200 + {status:"ok"} if we appear signed in
      - 202 + {status:"pending"} if still waiting
      - 401 + {status:"error"} if inline error is visible
    """
    _require_creds()

    try:
        ctx = await get_ctx()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("popup", lambda p: asyncio.create_task(p.close()))

        await page.goto(
            f"{URL_SHOPVOX}/sign-in", wait_until="domcontentloaded")

        # Ensure sign-in form fields are visible
        await page.locator("#email-input").wait_for(
            state="visible")
        await page.locator("#password-input").wait_for(
            state="visible")

        # Fill credentials
        await page.fill("#email-input", SHOPVOX_EMAIL)
        await page.fill("#password-input", SHOPVOX_PASSWORD)

        # Click Sign In WITHOUT expect_navigation (MFA keeps you on /sign-in)
        await page.locator("button.css-xdirqf").click()

        # Fast-path: check if MFA code field is visible now
        try:
            await page.locator("#otpCode-input").wait_for(state="visible", timeout=5000)
            return JSONResponse(
                content={"status": "mfa_required", "message": "MFA code requested"},
                status_code=202,
            )
        except PWTimeout:
            pass

        # If URL moved away from /sign-in, assume success
        if "/sign-in" not in page.url:
            return {"status": "ok", "message": "Logged in", "url": page.url}

        # Otherwise, inspect for inline error
        for sel in [
            ".css-oto7dz",
            "[data-testid='error'], .error, .alert-danger",
            "#email-field-wrapper.field-has-error",
            "#password-field-wrapper.field-has-error",
        ]:
            loc = page.locator(sel).first
            if await loc.is_visible():
                return JSONResponse(
                    content={
                        "status": "error",
                        "message": await loc.inner_text(),
                        "url": page.url,
                    },
                    status_code=401,
                )

        # Still on /sign-in with no obvious error and no MFA UI—treat as pending
        return JSONResponse(
            content={
                "status": "pending",
                "message": "Awaiting server response (no MFA UI or redirect yet)",
                "url": page.url,
            },
            status_code=202,
        )

    except PlaywrightError as e:
        raise HTTPException(status_code=500, detail=f"Playwright error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


@app.post("/login/shopvox/mfa")
async def login_mfa(body: MfaBodyModel):
    """
    Submit the 6-digit MFA code, optionally tick 'Trust this device', and finish sign-in.
    Returns:
      - 200 + {status:"ok"} when navigated away from /sign-in
      - 202 + {status:"pending"} if still waiting
      - 401 + {status:"error"} if inline errors remain
    """
    try:
        ctx = await get_ctx()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("popup", lambda p: asyncio.create_task(p.close()))

        # If we're already away from /sign-in, treat as success
        if "/sign-in" not in page.url:
            return {"status": "ok", "message": "Already signed in", "url": page.url}

        await page.locator("#otpCode-input").wait_for(
            state="visible", timeout=body.timeout_ms
        )

        # Enter code
        await page.fill("#otpCode-input", body.code)

        # Trust device checkbox (if present and requested)
        if body.trust_device:
            checkbox = page.locator('input[name="trustDevice"]')
            if await checkbox.count() > 0:
                if not await checkbox.is_checked():
                    await checkbox.check()

        # Click Sign In again
        await page.locator("button.css-xdirqf").click()

        # Wait for URL to change away from /sign-in
        try:
            await page.wait_for_url(
                lambda url: "/sign-in" not in url, timeout=body.timeout_ms
            )
            return {"status": "ok", "message": "MFA accepted", "url": page.url}
        except PWTimeout:
            # Check for inline error messaging
            for sel in [
                ".css-oto7dz",
                "[data-testid='error'], .error, .alert-danger",
                "#otpCode-field-wrapper.field-has-error",
            ]:
                loc = page.locator(sel).first
                if await loc.is_visible():
                    return JSONResponse(
                        content={
                            "status": "error",
                            "message": await loc.inner_text(),
                            "url": page.url,
                        },
                        status_code=401,
                    )
            # Still no redirect—treat as pending
            return JSONResponse(
                content={
                    "status": "pending",
                    "message": "Submission received; still waiting on server",
                    "url": page.url,
                },
                status_code=202,
            )

    except PlaywrightError as e:
        raise HTTPException(status_code=500, detail=f"Playwright error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


@app.get("/overdue-jobs")
async def get_overdue_jobs(background_tasks: BackgroundTasks):
    """
    Trigger the automation, return the PDF file, and clean it up afterward.
    """
    result = await fetch_overdue_jobs()

    if isinstance(result, dict):
        return JSONResponse(content=result, status_code=500)

    pdf_path: str = result
    background_tasks.add_task(safe_remove, pdf_path)

    if pdf_path == "":
        return JSONResponse(content={"message": "no rows found"}, status_code=204)

    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=os.path.basename(pdf_path),
        background=background_tasks,
    )


@app.get("/pending-jobs")
async def get_pending_jobs(
    background_tasks: BackgroundTasks,
    filters_model: JobFiltersModel = Depends(),
):
    filters: JobFilters = {}
    if filters_model.sales_rep is not None:
        filters["sales_rep"] = filters_model.sales_rep

    result = await fetch_pending_jobs(filters)

    if isinstance(result, dict):
        return JSONResponse(content=result, status_code=500)

    pdf_path: str = result
    if pdf_path == "":
        return JSONResponse(content={"message": "no rows found"}, status_code=204)

    background_tasks.add_task(safe_remove, pdf_path)
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=os.path.basename(pdf_path),
        background=background_tasks,
    )


@app.get("/login/sanmar")
async def login_sanmar():
    try:
        ctx = await get_ctx()
        page = await ctx.new_page()
        page.on("popup", lambda p: asyncio.create_task(p.close()))

        await page.goto(
            URL_SANMAR, wait_until="domcontentloaded")

        await page.fill("#username", SANMAR_USERNAME)
        await page.fill("#password", SANMAR_PASSWORD)
        await page.locator("input.form-check-input").click()

        await page.locator(
            "button.btn-df.btn-primary-df.btn-sm-df.text-nowrap.d-none.d-lg-inline-block"
        ).click()

        await page.wait_for_load_state("networkidle")
        await page.close()

        return JSONResponse(
            content={
                "message": "Successfully logged in",
            },
            status_code=200,
        )

    except PlaywrightError as e:
        raise HTTPException(status_code=500, detail=f"Playwright error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")

@app.get("/login/ss")
async def login_ss():
    ctx = await get_ctx()
    page = await ctx.new_page()
    await s_and_s.login(page)
    await page.close()

    return JSONResponse(
        content={
            "message": "Successfully logged in",
        },
        status_code=200,
    )

@app.get("/to-order")
async def get_to_order_so():

    result = await fetch_to_order_so()
    if len(result) <= 0:
        return JSONResponse(content={"message": "no rows found"}, status_code=204)
    return JSONResponse(content={"result": result}, status_code=200)


@app.post("/add-to-cart")
async def add_to_cart_r(orders: List[SalesOrder]):
    result = await add_to_cart(orders)
    return JSONResponse(content={"result": result}, status_code=200)

@app.post("/update-so-tag-ordered")
async def update_so_tag(orders: List[str]):

    ctx = await get_ctx()
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()

    await clean_not_order_yet_tags(page, orders)
    return JSONResponse(content={"message": "Updated"}, status_code=200)


@app.get("/")
async def hello():
    return {"time": datetime.datetime.now().isoformat()}
