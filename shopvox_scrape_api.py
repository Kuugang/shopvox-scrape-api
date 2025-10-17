import asyncio
import datetime
import os
import tempfile
from typing import Union

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

load_dotenv()

PW_CDP_URL = os.getenv("CDP_URL", "http://127.0.0.1:9222")
URL_SHOPVOX = "https://express.shopvox.com"
SHOPVOX_EMAIL = os.getenv("SHOPVOX_EMAIL")
SHOPVOX_PASSWORD = os.getenv("SHOPVOX_PASSWORD")
SHOPVOX_NEXT_URL = os.getenv("SHOPVOX_NEXT_URL")  # optional
TIMEOUT_MS_DEFAULT = int(os.getenv("SHOPVOX_TIMEOUT_MS", "15000"))

app = FastAPI(title="ShopVox Scrape API", version="1")


async def _get_persistent_context(pw):
    browser = await pw.chromium.connect_over_cdp(PW_CDP_URL)
    if not browser.contexts:
        raise RuntimeError(
            "No persistent browser context found. Start Chrome with --user-data-dir."
        )
    return browser.contexts[0]


def _require_creds():
    if not SHOPVOX_EMAIL or not SHOPVOX_PASSWORD:
        raise HTTPException(
            status_code=400,
            detail="Missing SHOPVOX_EMAIL or SHOPVOX_PASSWORD in .env",
        )


def safe_remove(path: str):
    """Safely remove temp file if it exists."""
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"⚠️ Cleanup failed for {path}: {e}")


@app.post("/login")
async def login():
    """
    Fill creds and click Sign In.
    Does NOT block on a full redirect; instead checks quickly if MFA is required.
    Returns:
      - {status:"mfa_required"} with 202 if OTP UI is shown
      - {status:"ok"} if we appear signed in (URL changed away from /sign-in)
      - {status:"error"} with 401 if inline error visible
    """
    _require_creds()
    timeout_ms = TIMEOUT_MS_DEFAULT
    next_url = SHOPVOX_NEXT_URL or f"{URL_SHOPVOX}/sign-in"

    async with async_playwright() as pw:
        try:
            ctx = await _get_persistent_context(pw)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            page.on("popup", lambda p: asyncio.create_task(p.close()))

            await page.goto(next_url, wait_until="domcontentloaded", timeout=timeout_ms)

            # Ensure sign-in form fields are visible
            await page.locator("#email-input").wait_for(
                state="visible", timeout=timeout_ms
            )
            await page.locator("#password-input").wait_for(
                state="visible", timeout=timeout_ms
            )

            # Fill credentials
            await page.fill("#email-input", SHOPVOX_EMAIL)
            await page.fill("#password-input", SHOPVOX_PASSWORD)

            # Click Sign In WITHOUT expect_navigation (MFA keeps you on /sign-in)
            await page.locator("button.css-xdirqf").click()

            # Fast-path check: is MFA code field now visible?
            try:
                await page.locator("#otpCode-input").wait_for(
                    state="visible", timeout=5000
                )
                # Optional: also check the red MFA banner is present
                # await page.locator("._alert_xinjw_1._red_xinjw_21").wait_for(timeout=1000)
                return JSONResponse(
                    content={"status": "mfa_required", "message": "MFA code requested"},
                    status_code=202,
                )
            except PWTimeout:
                pass  # not visible yet; maybe we logged in immediately

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


class MfaBody:
    code: str
    trust_device: bool = True
    timeout_ms: int = TIMEOUT_MS_DEFAULT


from pydantic import BaseModel


class MfaBodyModel(BaseModel):
    code: str
    trust_device: bool = True
    timeout_ms: int = TIMEOUT_MS_DEFAULT


@app.post("/login-mfa")
async def login_mfa(body: MfaBodyModel):
    """
    Submit the 6-digit MFA code, optionally tick 'Trust this device', and finish sign-in.
    Returns:
      - {status:"ok"} when navigated away from /sign-in
      - {status:"error"} 401 if inline errors remain
    """
    async with async_playwright() as pw:
        try:
            ctx = await _get_persistent_context(pw)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            page.on("popup", lambda p: asyncio.create_task(p.close()))

            # Ensure we're on the sign-in page and OTP field is present
            if "/sign-in" not in page.url:
                # already logged in?
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
                    checked = await checkbox.is_checked()
                    if not checked:
                        await checkbox.check()

            # Click Sign In again
            await page.locator("button.css-xdirqf").click()

            # Wait briefly for either redirect or inline error
            try:
                # Wait for URL to change away from /sign-in
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
    async with async_playwright() as pw:
        try:

            browser = await pw.chromium.connect_over_cdp(PW_CDP_URL)
            if not browser.contexts:
                return {
                    "error": "No persistent context found. Start Chrome with --user-data-dir"
                }
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            page.on("popup", lambda p: asyncio.create_task(p.close()))

            await page.goto(
                URL_SHOPVOX + "/jobs?view=f60b58c5-eb32-461b-9fed-05d6ac6d9ce3"
            )
            await page.locator("span:has-text('Jobs')").wait_for(state="visible")
            await page.wait_for_timeout(4000)
            await page.locator("button.css-obi7n2").click()
            await page.locator(
                "div.display-b.textDecoration-n.cursor-p.text-black"
            ).nth(1).click()

            # Capture the download
            async with page.expect_download() as download_info:
                await page.locator("button.css-xdirqf").click()
            download = await download_info.value

            tmp_dir = tempfile.gettempdir()
            pdf_path = os.path.join(tmp_dir, download.suggested_filename)
            await download.save_as(pdf_path)

            return pdf_path

        except PlaywrightError as e:
            return {"error": f"Playwright error: {str(e)}"}
        except Exception as e:
            return {"error": f"Unexpected error: {str(e)}"}


@app.get("/overdue-jobs")
async def get_overdue_jobs(background_tasks: BackgroundTasks):
    """Trigger Playwright scrape automation, return PDF, and clean up safely."""
    result = await fetch_overdue_jobs()

    if isinstance(result, dict):
        return JSONResponse(content=result, status_code=500)

    pdf_path: str = result
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
