#!/usr/bin/env python3
"""
Manga Chapter Scraper
---------------------
Uses a real Chromium browser (via Playwright) to bypass DDoS / bot-detection
protections (Cloudflare, etc.) and downloads manga chapter images in order.

Output: one folder per chapter containing numbered .jpg images + a combined PDF.

Usage:
    python3 scraper.py                    # scrape all URLs in urls.txt
    python3 scraper.py <url> [<url> ...]  # scrape specific URLs
"""

import asyncio
import sys
import re
import time
import random
import shutil
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
import img2pdf

# ── Config ────────────────────────────────────────────────────────────────────

URLS_FILE = "urls.txt"
OUTPUT_DIR = Path("downloads")

# How long to wait for page to fully settle (ms)
PAGE_LOAD_TIMEOUT = 60_000
# Extra wait after load so lazy-loaded images can appear (seconds)
SETTLE_WAIT = 4
# Delay between requests to avoid rate-limiting (seconds)
REQUEST_DELAY = (2.0, 4.5)
# Retries per image download
IMAGE_RETRIES = 3

# Realistic browser headers
EXTRA_HEADERS = {
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Sec-Fetch-Dest": "image",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "same-origin",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Turn a URL / title into a safe folder name."""
    text = re.sub(r"https?://[^/]+", "", text)
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_")
    return text[:120]


def chapter_dir(url: str) -> Path:
    parsed = urlparse(url)
    # e.g. /manga/super-gold-system/chapter-2  →  super-gold-system_chapter-2
    slug = slugify(parsed.netloc + parsed.path)
    d = OUTPUT_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def human_delay(lo: float = None, hi: float = None) -> None:
    lo = lo or REQUEST_DELAY[0]
    hi = hi or REQUEST_DELAY[1]
    time.sleep(random.uniform(lo, hi))


# ── Image selectors (ordered by specificity / likelihood) ────────────────────

IMAGE_SELECTORS = [
    # Common manga readers
    ".reading-content img",
    ".chapter-content img",
    ".manga-reader img",
    "#chapter-content img",
    ".page-break img",
    ".wp-manga-chapter-img",
    "img.wp-manga-chapter-img",
    ".reader-area img",
    ".viewer img",
    ".pages img",
    # Vortex / generic
    "div.container img",
    "main img",
    # Fallback — every img on the page
    "img",
]


async def find_images(page) -> list[str]:
    """Try each selector until we get a non-trivial list of manga page img URLs.

    Filters out small images (icons, buttons, UI) by checking naturalWidth /
    naturalHeight rendered in the browser — anything under 300 px wide is
    almost certainly not a manga page.
    """
    MIN_WIDTH = 300   # pixels — manga pages are always wider than this
    MIN_HEIGHT = 200  # pixels

    for selector in IMAGE_SELECTORS:
        candidates = await page.evaluate(
            """([sel, minW, minH]) => {
                const imgs = Array.from(document.querySelectorAll(sel));
                const results = [];
                for (const i of imgs) {
                    const src = i.dataset.src || i.dataset.lazySrc
                                || i.dataset.original || i.getAttribute('src') || '';
                    if (!src || src.startsWith('data:') || src.length < 10) continue;
                    // Must look like an image file
                    if (!/\\.(jpg|jpeg|png|webp)(\\?.*)?$/i.test(src)) continue;
                    // Dimension check — skip tiny icons / UI elements
                    const w = i.naturalWidth  || i.width  || 0;
                    const h = i.naturalHeight || i.height || 0;
                    if (w > 0 && w < minW) continue;
                    if (h > 0 && h < minH) continue;
                    results.push(src);
                }
                // De-duplicate while preserving order
                return [...new Set(results)];
            }""",
            [selector, MIN_WIDTH, MIN_HEIGHT],
        )

        if len(candidates) >= 2:
            print(f"  Found {len(candidates)} images with selector: {selector!r}")
            return candidates

    return []


# ── Downloader ────────────────────────────────────────────────────────────────

def download_image(url: str, dest: Path, session: requests.Session, referer: str) -> bool:
    """Download a single image with retries. Returns True on success."""
    headers = {**EXTRA_HEADERS, "Referer": referer}
    for attempt in range(1, IMAGE_RETRIES + 1):
        try:
            resp = session.get(url, headers=headers, timeout=30, stream=True)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            # Validate the image
            with Image.open(dest) as img:
                img.verify()
            return True
        except Exception as e:
            print(f"    Attempt {attempt}/{IMAGE_RETRIES} failed for {url}: {e}")
            if dest.exists():
                dest.unlink()
            if attempt < IMAGE_RETRIES:
                human_delay(1.5, 3.0)
    return False


def build_pdf(chapter_path: Path) -> Path:
    """Combine all downloaded images into a single PDF preserving order."""
    images = sorted(chapter_path.glob("*.jpg")) + sorted(chapter_path.glob("*.png")) + \
             sorted(chapter_path.glob("*.webp"))
    # re-sort numerically
    images = sorted(set(images), key=lambda p: int(re.search(r"(\d+)", p.stem).group(1))
                    if re.search(r"(\d+)", p.stem) else 0)

    if not images:
        print("  No images to bundle into PDF.")
        return None

    # Convert webp → jpg for img2pdf compatibility
    converted = []
    for img_path in images:
        if img_path.suffix.lower() == ".webp":
            jpg_path = img_path.with_suffix(".jpg")
            with Image.open(img_path) as im:
                im.convert("RGB").save(jpg_path, "JPEG", quality=95)
            converted.append(jpg_path)
        else:
            converted.append(img_path)

    pdf_path = chapter_path / "chapter.pdf"
    with open(pdf_path, "wb") as f:
        f.write(img2pdf.convert([str(p) for p in converted]))

    print(f"  PDF saved: {pdf_path} ({len(converted)} pages)")
    return pdf_path


# ── Core scraper ──────────────────────────────────────────────────────────────

async def scrape_chapter(url: str, browser) -> bool:
    out_dir = chapter_dir(url)
    print(f"\n{'─'*60}")
    print(f"Chapter URL : {url}")
    print(f"Output dir  : {out_dir}")

    context = await browser.new_context(
        viewport={"width": 1366, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="America/New_York",
        # Don't reveal we're headless
        java_script_enabled=True,
    )

    # Mask automation signals
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        window.chrome = { runtime: {} };
    """)

    page = await context.new_page()

    try:
        print("  Loading page …")
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)

        # Wait for network to quiet down + any lazy loaders
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout:
            pass  # networkidle timed out — continue anyway

        # Scroll to trigger lazy-load
        print("  Scrolling to trigger lazy-load …")
        for _ in range(5):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(0.8)
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(SETTLE_WAIT)

        image_urls = await find_images(page)
        if not image_urls:
            print("  ✗ No chapter images found — the page structure may be unusual.")
            print("  Saving a screenshot for manual inspection …")
            await page.screenshot(path=str(out_dir / "debug_screenshot.png"), full_page=True)
            return False

        # Grab cookies from the browser context and feed them into requests
        cookies = await context.cookies()
        session = requests.Session()
        for c in cookies:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

        print(f"  Downloading {len(image_urls)} images …")
        ok_count = 0
        for idx, img_url in enumerate(image_urls, start=1):
            # Handle relative URLs
            if img_url.startswith("//"):
                img_url = "https:" + img_url
            elif img_url.startswith("/"):
                parsed = urlparse(url)
                img_url = f"{parsed.scheme}://{parsed.netloc}{img_url}"

            ext = re.search(r"\.(jpg|jpeg|png|webp)", img_url, re.I)
            ext = ext.group(0).lower() if ext else ".jpg"
            dest = out_dir / f"{idx:04d}{ext}"

            if dest.exists():
                print(f"  [{idx:>3}/{len(image_urls)}] Already exists, skipping.")
                ok_count += 1
                continue

            success = download_image(img_url, dest, session, referer=url)
            if success:
                print(f"  [{idx:>3}/{len(image_urls)}] ✓ {dest.name}")
                ok_count += 1
            else:
                print(f"  [{idx:>3}/{len(image_urls)}] ✗ Failed: {img_url}")

            human_delay()

        print(f"\n  Downloaded {ok_count}/{len(image_urls)} images.")

        if ok_count > 0:
            build_pdf(out_dir)

        return ok_count == len(image_urls)

    finally:
        await page.close()
        await context.close()


async def main(urls: list[str]) -> None:
    print("Starting manga scraper …")
    print(f"Output directory: {OUTPUT_DIR.resolve()}\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--window-size=1366,900",
            ],
        )

        results = {}
        for url in urls:
            url = url.strip()
            if not url:
                continue
            success = await scrape_chapter(url, browser)
            results[url] = success
            if len(urls) > 1:
                human_delay(3.0, 6.0)  # pause between chapters

        await browser.close()

    print("\n" + "═" * 60)
    print("Summary:")
    for url, ok in results.items():
        status = "✓ OK" if ok else "✗ Issues"
        print(f"  {status}  {url}")
    print("═" * 60)

    if all(results.values()):
        print("\nAll chapters downloaded successfully.")
    else:
        print("\nSome chapters had issues — check the output above.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1:
        target_urls = sys.argv[1:]
    else:
        if not Path(URLS_FILE).exists():
            print(f"No {URLS_FILE} found and no URLs given as arguments.")
            sys.exit(1)
        target_urls = [u for u in Path(URLS_FILE).read_text().splitlines() if u.strip()]

    if not target_urls:
        print("No URLs to scrape.")
        sys.exit(1)

    asyncio.run(main(target_urls))
