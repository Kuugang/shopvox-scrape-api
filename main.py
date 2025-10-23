import asyncio
import datetime
import os
import re
import tempfile
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Union

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from playwright.async_api import BrowserContext
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Playwright
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

from schemas import JobFilters, JobFiltersModel, MfaBodyModel

load_dotenv()

URL_SHOPVOX = "https://express.shopvox.com"
SHOPVOX_EMAIL = os.getenv("SHOPVOX_EMAIL", "")
SHOPVOX_PASSWORD = os.getenv("SHOPVOX_PASSWORD", "")
SHOPVOX_NEXT_URL = os.getenv("SHOPVOX_NEXT_URL")
TIMEOUT_MS_DEFAULT = int(os.getenv("SHOPVOX_TIMEOUT_MS", "15000"))

USER_DATA_DIR = os.getenv("PW_USER_DATA_DIR", "./profile")
HEADLESS = os.getenv("PW_HEADLESS", "true").lower() != "false"

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


app = FastAPI(title="ShopVox Scrape API", version="1", lifespan=lifespan)


# ===== Routes =====
@app.post("/login")
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
    timeout_ms = TIMEOUT_MS_DEFAULT
    next_url = SHOPVOX_NEXT_URL or f"{URL_SHOPVOX}/sign-in"

    try:
        ctx = await get_ctx()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("popup", lambda p: asyncio.create_task(p.close()))

        await page.goto(next_url, wait_until="domcontentloaded", timeout=timeout_ms)

        # Ensure sign-in form fields are visible
        await page.locator("#email-input").wait_for(state="visible", timeout=timeout_ms)
        await page.locator("#password-input").wait_for(
            state="visible", timeout=timeout_ms
        )

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


@app.post("/login-mfa")
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


@app.get("/")
async def hello():
    return {"time": datetime.datetime.now().isoformat()}
