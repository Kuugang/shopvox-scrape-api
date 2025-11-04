import os
import re
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
            await page.goto("about:blank")
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


def _normalize_size(size: str) -> str:
    """
    Map arbitrary size strings to a universal size:
    XS, S, M, L, XL, 2XL, 3XL, ...; OSFA for one-size; OTHER if unknown.
    """
    if not size:
        return "OTHER"

    s = size.strip().lower()

    # Quick one-size checks
    if re.search(r"\b(osfa|one\s*size(\s*f(its)?\s*all)?|o/s|os)\b", s):
        return "OSFA"

    # Drop non-size descriptors (gender/age/fit/etc.)
    s = re.sub(
        r"\b(womens?|ladies|mens?|unisex|adult|youth|kids?|junior|toddler|infant)\b",
        "",
        s,
    )
    s = re.sub(r"\b(w|m|f)\b", "", s)  # leftover single-letter gender marks
    s = s.replace("’", "'").replace("‘", "'")

    # Normalize separators & spaces
    s = re.sub(r"[._\-\/]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    # Canonical words -> letters
    s = re.sub(r"\b(extra\s*)?small\b", "xs", s)
    s = re.sub(r"\bmedium\b", "m", s)
    s = re.sub(r"\b(extra\s*)?large\b", "xl", s)

    # Patterns like "xxl", "xxxl", "xxxxl"
    m = re.search(r"\b(x{2,})\s*l\b", s)  # xx l, xxx l
    if m:
        n = len(m.group(1))
        return f"{n}XL" if n >= 2 else "XL"

    # Patterns like "2xl", "3 xl", "4x large"
    m = re.search(r"\b(\d+)\s*x\s*l\b", s)  # 2 x l
    if m:
        return f"{int(m.group(1))}XL"

    m = re.search(r"\b(\d+)\s*xl\b", s)  # 2xl
    if m:
        return f"{int(m.group(1))}XL"

    # Single X large variants: "x l", "x-large", "xlarge"
    if re.search(r"\bx\s*l\b|\bx-?large\b|\bxlarge\b", s):
        return "XL"

    # XS variants: "xsmall", "x-small", "xs"
    if re.search(r"\bxxs\b|\bxx-small\b|\bextra\s*extra\s*small\b", s):
        return "XS"  # collapse 2XS -> XS for universal set
    if re.search(r"\bxs\b|\bx-?small\b|\bxsmall\b", s):
        return "XS"

    # Plain S / M / L
    if re.search(r"\bs\b(?![a-z])|\bsmall\b", s):
        return "S"
    if re.search(r"\bm\b(?![a-z])|\bmedium\b|^md\b", s):
        return "M"
    if re.search(r"\bl\b(?![a-z])|\blarge\b|^lg\b", s):
        return "L"

    # If it’s exactly an XL token after normalization
    if re.fullmatch(r"xl", s):
        return "XL"

    # Sometimes strings end with the size token (e.g., "mens 2x-large")
    tail = s.split()[-1]
    # Try tail as last resort
    if tail in {"xs", "s", "m", "l", "xl"}:
        return tail.upper()
    m = re.fullmatch(r"(\d+)xl", tail)
    if m:
        return f"{int(m.group(1))}XL"

    # One more: "2x", "3x" without "l"
    m = re.fullmatch(r"(\d+)x", tail)
    if m:
        return f"{int(m.group(1))}XL"

    # "one size" variants already handled; default
    return "OTHER"
