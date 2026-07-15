from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from playwright.async_api import async_playwright, Page
import asyncio
import json
import subprocess
import time
import os
from urllib.parse import quote, urlparse, parse_qs, urlencode
import logging
from datetime import datetime
import requests

# Configure logging
log_filename = f"zepto_orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI()

class OrderRequest(BaseModel):
    products: List[str]
    card_number: Optional[str] = ""
    card_expiry: Optional[str] = ""
    card_cvv: Optional[str] = ""

async def handle_popups(page: Page):
    """Handle any popups by pressing escape and clicking close buttons."""
    try:
        # Check for common popup indicators
        popup_selectors = [
            # Super Saver specific selectors
            'div[style*="cart_supersaver_prominent_nudge_bg.png"] button',  # Super saver close button
            'button:has(svg path[stroke="#fff"])',  # Close button with white X icon
            'button:has-text("✕")',  # Close button with × symbol
            
            # General popup selectors
            'div[role="dialog"]',  # Common dialog/modal
            '.modal',  # Common modal class
            '[class*="popup"]',  # Any element with popup in class
            '[class*="modal"]',  # Any element with modal in class
            '.Super-Saver',  # Super saver popup
            '[class*="super-saver"]'  # Super saver related elements
        ]
        
        for selector in popup_selectors:
            try:
                popup = await page.wait_for_selector(selector, timeout=2000)
                if popup:
                    logger.info(f"Found popup with selector: {selector}")
                    # Try clicking the close button first
                    try:
                        await popup.click()
                        logger.info("Clicked popup close button")
                    except Exception:
                        # If clicking fails, try pressing escape
                        await page.keyboard.press('Escape')
                        logger.info("Pressed escape to close popup")
                    
                    await page.wait_for_timeout(500)  # Wait for popup animation
                    
                    # Verify if popup was closed
                    try:
                        is_visible = await popup.is_visible()
                        if is_visible:
                            logger.warning("Popup might still be visible after closing attempt")
                    except Exception:
                        logger.info("Popup appears to be closed")
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Error handling popups: {e}")

from playwright.async_api import Page, Locator  # make sure this import is at top

async def find_add_to_cart_button(page: Page, product_name: str) -> bool:
    await page.wait_for_timeout(2000)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(300)

    query = product_name.lower().strip()
    keywords = [k for k in query.replace("(", "").replace(")", "").replace("pc", "").replace("g", "").split() if len(k) > 2]

    # All product titles
    title_links = page.locator("a[href*='/pn/']")
    # All ADD buttons (same order as cards)
    add_buttons = page.locator("button:has-text('ADD')")

    title_count = await title_links.count()
    add_count = await add_buttons.count()

    logger.info(f"Found {title_count} product titles and {add_count} ADD buttons")

    count = min(title_count, add_count)

    best_idx = -1
    best_score = 0

    for i in range(count):
        title_text = (await title_links.nth(i).inner_text()).lower()
        score = sum(1 for k in keywords if k in title_text)

        if score > best_score:
            best_score = score
            best_idx = i

    if best_idx == -1 or best_score < 2:
        logger.warning(f"No good match found for product: {product_name}")
        return False

    add_btn = add_buttons.nth(best_idx)

    if await add_btn.is_visible():
        await add_btn.click(force=True)
        logger.info(f"Clicked ADD for matched product: {product_name}")
        return True

    logger.warning(f"Matched ADD button not visible for product: {product_name}")
    return False

async def open_cart(page: Page):
    """Open the cart: first try 'Go to Cart' popup, then header cart icon."""
    try:
        logger.info("Trying to open cart (popup, then header cart icon)")

        # 1) First try: the 'Go to Cart' button in the Added to Cart popup
        popup_selectors = [
            "button:has-text('Go to Cart')",
            "button:has-text('Go to Cart >')",
            "div:has-text('Go to Cart')",
            "div:has-text('Go to Cart >')"
        ]

        for sel in popup_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible():
                    logger.info(f"Clicking 'Go to Cart' using selector: {sel}")
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    return True
            except Exception:
                continue

        logger.info("'Go to Cart' popup button not found, trying header cart icon")

        # 2) Fallback: click the Cart icon / link in the header
        cart_selectors = [
            "a[href*='/cart']",
            "a:has-text('Cart')",
            "button:has-text('Cart')",
            "div:has-text('Cart')"
        ]

        for sel in cart_selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible():
                    logger.info(f"Clicking cart header icon using selector: {sel}")
                    await el.click()
                    await page.wait_for_timeout(3000)
                    return True
            except Exception:
                continue

        logger.error("Could not find any cart button/icon")
        return False

    except Exception as e:
        logger.error(f"Error opening cart: {e}")
        return False

def is_chrome_running():
    """Check if Chrome is already running with remote debugging enabled."""
    try:
        response = requests.get("http://localhost:9222/json/version", timeout=2)
        return response.status_code == 200
    except:
        return False

async def ensure_chrome_running():
    """
    Windows-only: force-start Google Chrome with remote debugging.
    This version is simplified for your setup.
    """
    try:
        logger.info("Force starting Chrome with remote debugging (Windows)")

        # 1) Kill any existing Chrome
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "chrome.exe"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1)
            logger.info("Killed existing chrome.exe processes (if any)")
        except Exception as e:
            logger.warning(f"Error killing Chrome (can ignore if none running): {e}")

        # 2) Use your real Chrome path
        chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

        if not os.path.exists(chrome_path):
            logger.error(f"Chrome not found at {chrome_path}")
            return False

        # 3) Use a dedicated profile directory
        user_data_dir = os.path.join(
            os.getenv("TEMP", r"C:\Temp"),
            "zepto_chrome_profile"
        )

        # 4) Start Chrome with remote debugging
        subprocess.Popen([
            chrome_path,
            "--remote-debugging-port=9222",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={user_data_dir}",
            "https://www.zeptonow.com"
        ])

        # 5) Give Chrome some time to start
        logger.info("Waiting 8 seconds for Chrome to start...")
        time.sleep(8)

        # Don’t over-check – let Playwright handle connection retries
        logger.info("Assuming Chrome is running with remote debugging")
        return True

    except Exception as e:
        logger.error(f"Error ensuring Chrome is running (Windows simplified): {e}")
        return False

async def enter_upi_and_pay(page: Page, card_number: str, card_expiry: str, card_cvv: str):
    """Switch to Juspay tab and enter card details."""
    try:
        logger.info("Waiting for Juspay payment page to open...")

        # Juspay opens in a NEW TAB — find it in the browser context
        context = page.context
        juspay_page = None

        # Wait up to 15 seconds for the Juspay tab to appear
        for _ in range(30):
            await page.wait_for_timeout(500)
            for p in context.pages:
                if 'juspay' in p.url or 'payments' in p.url:
                    juspay_page = p
                    break
            if juspay_page:
                break

        if not juspay_page:
            logger.warning("No Juspay tab found, checking if current page navigated to Juspay")
            if 'juspay' in page.url or 'payments' in page.url:
                juspay_page = page
            else:
                raise Exception("Juspay payment page did not open")

        logger.info(f"Found Juspay page: {juspay_page.url}")
        await juspay_page.bring_to_front()
        await juspay_page.wait_for_load_state("domcontentloaded")
        await juspay_page.wait_for_timeout(2000)

        # Click "Credit / Debit Card" tab on Juspay if not already selected
        try:
            card_tab = juspay_page.locator("text=Credit / Debit Card").first
            if await card_tab.is_visible():
                await card_tab.click()
                logger.info("Clicked Credit/Debit Card tab on Juspay")
                await juspay_page.wait_for_timeout(1000)
        except Exception:
            logger.info("Credit/Debit Card tab not found or already selected")

        # Juspay card fields may be inside iframes — search page + all frames
        def get_all_frames(p):
            frames = [p.main_frame]
            for frame in p.frames:
                if frame not in frames:
                    frames.append(frame)
            return frames

        async def find_input_in_frames(page_obj, selectors, field_name):
            """Try selectors on main page first, then each iframe."""
            # Try main page
            for sel in selectors:
                try:
                    el = await page_obj.wait_for_selector(sel, timeout=3000)
                    if el:
                        logger.info(f"Found {field_name} on main page: {sel}")
                        return el, page_obj
                except Exception:
                    continue
            # Try each frame
            for frame in page_obj.frames:
                for sel in selectors:
                    try:
                        el = await frame.wait_for_selector(sel, timeout=2000)
                        if el:
                            logger.info(f"Found {field_name} in iframe ({frame.url}): {sel}")
                            return el, frame
                    except Exception:
                        continue
            return None, None

        async def type_into_frame_field(frame_or_page, element, text):
            """Type char by char into a field, works for both Page and Frame."""
            await element.click()
            await juspay_page.wait_for_timeout(300)
            await element.press("Control+a")
            await element.press("Backspace")
            await juspay_page.wait_for_timeout(200)
            for char in text:
                await element.press(char)
                await juspay_page.wait_for_timeout(50)

        # Fill Card Number
        card_selectors = [
            'input[placeholder="Enter Card Number"]',
            'input[placeholder*="Card Number"]',
            'input[autocomplete="cc-number"]',
            'input[name="card_number"]',
            'input[id*="card"]',
            'input[type="tel"]',
        ]
        card_input, card_frame = await find_input_in_frames(juspay_page, card_selectors, "card number")
        if not card_input:
            raise Exception("Card number input not found on Juspay page or iframes")
        await type_into_frame_field(card_frame, card_input, card_number)
        logger.info("Entered card number")
        await juspay_page.wait_for_timeout(600)

        # Fill Expiry
        expiry_selectors = [
            'input[placeholder="MM/YY"]',
            'input[placeholder*="MM"]',
            'input[autocomplete="cc-exp"]',
            'input[name="expiry"]',
            'input[id*="expiry"]',
            'input[id*="exp"]',
        ]
        expiry_input, expiry_frame = await find_input_in_frames(juspay_page, expiry_selectors, "expiry")
        if not expiry_input:
            raise Exception("Expiry input not found on Juspay page or iframes")
        await type_into_frame_field(expiry_frame, expiry_input, card_expiry)
        logger.info("Entered expiry")
        await juspay_page.wait_for_timeout(600)

        # Fill CVV
        cvv_selectors = [
            'input[placeholder="CVV"]',
            'input[placeholder*="CVV"]',
            'input[placeholder*="CVC"]',
            'input[autocomplete="cc-csc"]',
            'input[name="cvv"]',
            'input[id*="cvv"]',
        ]
        cvv_input, cvv_frame = await find_input_in_frames(juspay_page, cvv_selectors, "CVV")
        if not cvv_input:
            raise Exception("CVV input not found on Juspay page or iframes")
        await type_into_frame_field(cvv_frame, cvv_input, card_cvv)
        logger.info("Entered CVV")
        await juspay_page.wait_for_timeout(600)

        # Click Proceed to Pay (on main page)
        pay_selectors = [
            'button:has-text("Proceed to Pay")',
            'button:has-text("Pay Now")',
            'button:has-text("Make Payment")',
        ]
        pay_btn = None
        for sel in pay_selectors:
            try:
                pay_btn = await juspay_page.wait_for_selector(sel, timeout=4000)
                if pay_btn and await pay_btn.is_visible():
                    logger.info(f"Found pay button: {sel}")
                    break
            except Exception:
                continue
        if not pay_btn:
            raise Exception("Pay button not found on Juspay page")
        await pay_btn.click()
        logger.info("Clicked Proceed to Pay on Juspay")
        return True

    except Exception as e:
        logger.error(f"Error in card payment process: {e}")
        return False

@app.post("/order")
async def create_order(order: OrderRequest):
    order_id = datetime.now().strftime('%Y%m%d_%H%M%S')
    logger.info(f"Received new order {order_id} with products: {order.products}")

    # Ensure Chrome is running with retry logic
    max_retries = 3
    for attempt in range(max_retries):
        if await ensure_chrome_running():
            break
        logger.warning(f"Failed to ensure Chrome is running (attempt {attempt+1}/{max_retries})")
        if attempt == max_retries - 1:
            raise HTTPException(status_code=500, detail="Could not ensure Chrome is running")
        time.sleep(2)

    try:
        async with async_playwright() as p:
            logger.info("Initializing Playwright")
            
            # Add retry logic for connecting to Chrome
            max_connect_retries = 3
            browser = None
            
            for attempt in range(max_connect_retries):
                try:
                    logger.info(f"Connecting to Chrome (attempt {attempt+1}/{max_connect_retries})")
                    browser = await p.chromium.connect_over_cdp('http://localhost:9222')
                    logger.info("Connected to Chrome instance")
                    break
                except Exception as e:
                    logger.warning(f"Failed to connect to Chrome: {e}")
                    if attempt < max_connect_retries - 1:
                        time.sleep(2)
                        # Try to restart Chrome by killing and starting fresh
                        try:
                            subprocess.run(['pkill', '-f', 'Google Chrome'])
                            time.sleep(2)
                        except:
                            pass
                        await ensure_chrome_running()
                        continue
                    else:
                        raise HTTPException(status_code=500, 
                                         detail=f"Failed to connect to Chrome after {max_connect_retries} attempts")
            
            if not browser:
                raise HTTPException(status_code=500, detail="Failed to connect to browser")
            
            # Get the default context (first one)
            contexts = browser.contexts
            if not contexts:
                logger.info("No existing context found, creating new page in default context")
                page = await browser.new_page()
            else:
                logger.info("Using existing browser context")
                context = contexts[0]
                pages = context.pages
                if pages:
                    page = pages[0]
                    logger.info("Using existing page")
                else:
                    logger.info("Creating new page in existing context")
                    page = await context.new_page()
            
            # Search and add each product
            successful_products = []
            failed_products = []
            
            for product in order.products:
                try:
                    logger.info(f"Processing product: {product}")
                    # Directly navigate to search URL
                    encoded_product = quote(product)
                    search_url = f'https://www.zeptonow.com/search?query={encoded_product}'
                    logger.info(f"Navigating to search URL: {search_url}")
                    await page.goto(search_url, wait_until='domcontentloaded')
                    await page.evaluate("window.scrollTo(0, 0)")
                    await page.wait_for_timeout(3000)
# Handle popups
                    await handle_popups(page)

                    logger.info("Attempting to find Add to Cart button")
                    clicked = await find_add_to_cart_button(page, product)

                    if not clicked:
                        raise HTTPException(status_code=404, detail=f"Exact product not found: {product}")
                except Exception as e:
                    logger.error(f"Error processing product {product}: {e}")
                    failed_products.append(product)
                    raise HTTPException(status_code=400, detail=f"Error adding product {product}: {str(e)}")
                
                logger.info("Proceeding to checkout")

# Handle popups before opening cart
                await handle_popups(page)

# Open the cart (uses Go to Cart popup or header Cart icon)
                if not await open_cart(page):
                    raise HTTPException(status_code=400, detail="Failed to open cart")

# Handle any popups that appear after cart opens
                await handle_popups(page)

                logger.info("Successfully opened cart")

            try:
                logger.info("Looking for payment button")

                payment_button = None

                payment_button_selectors = [
                    'button:has-text("Click to Pay")',
                    'div:has-text("Click to Pay")',
                    'button[testid="place-order-btn"]',
                    'button:has-text("Checkout")'
                ]

                for selector in payment_button_selectors:
                    try:
                        btn = await page.wait_for_selector(selector, timeout=2000)
                        if btn and await btn.is_visible():
                            logger.info(f"Found payment button using selector: {selector}")
                            payment_button = btn
                            await payment_button.click()
                            await page.wait_for_timeout(1500)
                            break
                    except Exception:
                        continue

                if not payment_button:
                    raise HTTPException(status_code=400, detail="Payment button not found")

                # Select Credit/Debit Card payment method
                logger.info("Waiting for payment options")
                await page.wait_for_selector('text=Credit / Debit Card', timeout=8000)
                await page.wait_for_timeout(1000)

                # NavBar tablist (id=20000076) intercepts pointer events — use JS dispatchEvent to bypass
                clicked = await page.evaluate("""
                    () => {
                        const articles = Array.from(document.querySelectorAll('article'));
                        const target = articles.find(el => el.textContent.trim() === 'Credit / Debit Card');
                        if (target) {
                            target.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                            return true;
                        }
                        // fallback: any element with exact text
                        const all = Array.from(document.querySelectorAll('*'));
                        const fallback = all.find(el =>
                            el.children.length === 0 &&
                            el.textContent.trim() === 'Credit / Debit Card'
                        );
                        if (fallback) {
                            fallback.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                            return true;
                        }
                        return false;
                    }
                """)
                if not clicked:
                    raise Exception("Could not find or click Credit / Debit Card option")
                logger.info("Clicked Credit/Debit Card via JS dispatchEvent")
                await page.wait_for_timeout(2000)

                # Enter UPI ID and click Verify and Pay
                if not await enter_upi_and_pay(page, order.card_number, order.card_expiry, order.card_cvv):
                    raise HTTPException(status_code=400, detail="Failed to process UPI payment")
                
                # Wait for a moment to ensure the payment process started
                await page.wait_for_timeout(2000)
                
                # Capture screenshot for verification
                screenshot_path = f'order_status_{order_id}.png'
                await page.screenshot(path=screenshot_path)
                logger.info(f"Captured order screenshot: {screenshot_path}")
                
                logger.info(f"Order {order_id} completed successfully")
                
                return {
                    "status": "success",
                    "order_id": order_id,
                    "message": "Order process completed and payment initiated",
                    "products_added": successful_products,
                    "products_failed": failed_products,
                    "card_used": order.card_number[-4] if order.card_number else "",
                    "screenshot": screenshot_path
                }
            except Exception as e:
                logger.error(f"Error during checkout: {e}")
                raise HTTPException(status_code=400, detail=f"Error during checkout: {str(e)}")
            
    except Exception as e:
        logger.error(f"Browser automation error: {e}")
        raise HTTPException(status_code=500, detail=f"Browser automation error: {str(e)}")

if __name__ == "__main__":
    logger.info("Starting Zepto Order Automation Server")
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
