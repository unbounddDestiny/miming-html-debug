#!/usr/bin/env python3
"""
Manga Chapter Scraper
---------------------
Uses a real Chromium browser (via Playwright) to bypass DDoS / bot-detection
protections (Cloudflare, etc.) and downloads manga chapter images in order.

Output: one folder per chapter containing numbered images + a combined PDF.

Speed design
------------
  • Bot-detection only lives on the *reader page* — we keep human-like delays
    there (scroll pauses, networkidle wait, settle time).
  • Image files are served from a CDN with no bot protection, so we download
    them in parallel (DOWNLOAD_CONCURRENCY at once) with no inter-image delay.
  • Multiple chapters are processed with limited concurrency (CHAPTER_CONCURRENCY)
    using separate browser contexts so sites can't correlate sessions.

Usage:
    python3 scraper.py                    # scrape all URLs in urls.txt
    python3 scraper.py <url> [<url> ...]  # scrape specific URLs
"""

import asyncio
import sys
import re
import time
import random
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
import img2pdf

# ── Config ────────────────────────────────────────────────────────────────────

URLS_FILE = "urls.txt"
OUTPUT_DIR = Path("downloads")

# Playwright page-load config (where bot detection actually lives)
PAGE_LOAD_TIMEOUT = 60_000        # ms
SETTLE_WAIT      = 4              # seconds after scrolling before reading DOM
SCROLL_STEPS     = 5
SCROLL_PAUSE     = 0.6            # seconds between scroll steps

# Image download config (CDN — no bot detection, safe to parallelise)
DOWNLOAD_CONCURRENCY = 5          # simultaneous image downloads per chapter
IMAGE_RETRIES        = 3          # retries per image on failure
IMAGE_TIMEOUT        = 30         # seconds per request
RETRY_DELAY          = (1.0, 2.0) # back-off between retries (seconds)

# Chapter-level concurrency (separate browser contexts per chapter)
CHAPTER_CONCURRENCY  = 2          # chapters scraped in parallel
INTER_CHAPTER_DELAY  = (2.0, 4.0) # seconds between chapter *starts*

# Realistic browser headers for image requests
IMG_HEADERS = {
    "Accept":          "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT":             "1",
    "Sec-Fetch-Dest":  "image",
    "Sec-Fetch-Mode":  "no-cors",
    "Sec-Fetch-Site":  "same-origin",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = re.sub(r"https?://[^/]+", "", text)
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_")
    return text[:120]


def chapter_dir(url: str) -> Path:
    parsed = urlparse(url)
    slug   = slugify(parsed.netloc + parsed.path)
    d      = OUTPUT_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def jitter(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


# ── Image selectors (specificity order) ──────────────────────────────────────

IMAGE_SELECTORS = [
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
    "div.container img",
    "main img",
    "img",
]


async def find_images(page) -> list[str]:
    """Return ordered, de-duplicated list of manga-page image URLs.

    Dimension filtering only fires when the image has *actually loaded*
    (naturalWidth > 0). Lazy-loaded placeholders have naturalWidth=0 but a
    tiny CSS height (e.g. 16 px) — using i.height instead of naturalHeight
    would incorrectly discard every manga page on sites like ManhuaPlus.
    """
    MIN_W, MIN_H = 300, 200

    for selector in IMAGE_SELECTORS:
        candidates = await page.evaluate(
            """([sel, minW, minH]) => {
                const imgs = Array.from(document.querySelectorAll(sel));
                const out  = [];
                for (const i of imgs) {
                    const src = i.dataset.src || i.dataset.lazySrc
                                || i.dataset.original || i.getAttribute('src') || '';
                    if (!src || src.startsWith('data:') || src.length < 10) continue;
                    if (!/\\.(jpg|jpeg|png|webp)(\\?.*)?$/i.test(src)) continue;
                    // Only apply size filter when the browser has actually loaded
                    // the image (naturalWidth > 0). Unloaded lazy images report
                    // naturalWidth=0 — we must keep those; the URL regex above
                    // already weeds out obvious non-image assets.
                    const nw = i.naturalWidth;
                    const nh = i.naturalHeight;
                    if (nw > 0 && nw < minW) continue;
                    if (nh > 0 && nh < minH) continue;
                    out.push(src);
                }
                return [...new Set(out)];
            }""",
            [selector, MIN_W, MIN_H],
        )
        if len(candidates) >= 2:
            print(f"  Found {len(candidates)} images ({selector!r})")
            return candidates

    return []


# ── Image downloader (runs in a thread pool) ──────────────────────────────────

def _download_blocking(url: str, dest: Path, session: requests.Session,
                       referer: str) -> bool:
    """Blocking download with retries. Called via asyncio.to_thread."""
    headers = {**IMG_HEADERS, "Referer": referer}
    for attempt in range(1, IMAGE_RETRIES + 1):
        try:
            resp = session.get(url, headers=headers,
                               timeout=IMAGE_TIMEOUT, stream=True)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            with Image.open(dest) as img:
                img.verify()
            return True
        except Exception as exc:
            print(f"    [{dest.name}] attempt {attempt}/{IMAGE_RETRIES} failed: {exc}")
            if dest.exists():
                dest.unlink()
            if attempt < IMAGE_RETRIES:
                jitter(*RETRY_DELAY)
    return False


async def download_image(url: str, dest: Path, session: requests.Session,
                         referer: str, sem: asyncio.Semaphore) -> bool:
    """Async wrapper — acquires semaphore then runs blocking download in thread."""
    async with sem:
        return await asyncio.to_thread(_download_blocking, url, dest, session, referer)


# ── PDF builder ───────────────────────────────────────────────────────────────

def build_pdf(chapter_path: Path) -> Path | None:
    """Compress every page image to JPEG q75 (~50% smaller, no visible loss),
    combine into a single PDF, then delete all the individual image files so
    only chapter.pdf remains.
    """
    JPEG_QUALITY = 75          # moderate — visually lossless, ~50% smaller
    JPEG_SUBSAMPLING = 0       # 4:4:4 chroma — preserves colour detail

    originals = sorted(
        set(chapter_path.glob("*.jpg")) |
        set(chapter_path.glob("*.png")) |
        set(chapter_path.glob("*.webp")),
        key=lambda p: int(m.group(1)) if (m := re.search(r"(\d+)", p.stem)) else 0,
    )
    if not originals:
        print("  No images to bundle into PDF.")
        return None

    tmp_dir = chapter_path / "_tmp_compressed"
    tmp_dir.mkdir(exist_ok=True)

    compressed = []
    original_bytes = 0
    compressed_bytes = 0

    for p in originals:
        out = tmp_dir / (p.stem + ".jpg")
        with Image.open(p) as im:
            original_bytes += p.stat().st_size
            im.convert("RGB").save(
                out, "JPEG",
                quality=JPEG_QUALITY,
                subsampling=JPEG_SUBSAMPLING,
                optimize=True,
            )
            compressed_bytes += out.stat().st_size
        compressed.append(out)

    pdf_path = chapter_path / "chapter.pdf"
    with open(pdf_path, "wb") as f:
        f.write(img2pdf.convert([str(p) for p in compressed]))

    # Clean up temp compressed files and all originals — keep only the PDF
    for p in compressed:
        p.unlink()
    tmp_dir.rmdir()
    for p in originals:
        if p.exists():
            p.unlink()
    # Also remove any leftover .jpg files created by earlier webp→jpg passes
    for p in chapter_path.glob("*.jpg"):
        if p != pdf_path:
            p.unlink()

    saving = (1 - compressed_bytes / original_bytes) * 100 if original_bytes else 0
    print(f"  PDF saved → {pdf_path}  ({len(originals)} pages, "
          f"{original_bytes/1024/1024:.1f} MB → "
          f"{pdf_path.stat().st_size/1024/1024:.1f} MB, "
          f"{saving:.0f}% smaller)")


# ── Core scraper ──────────────────────────────────────────────────────────────

async def scrape_chapter(url: str, browser,
                         start_delay: float = 0.0) -> bool:
    """Scrape one chapter. start_delay staggers parallel chapter starts."""
    if start_delay:
        await asyncio.sleep(start_delay)

    out_dir = chapter_dir(url)
    tag     = urlparse(url).path.split("/")[-2] or urlparse(url).path.rstrip("/").split("/")[-1]
    prefix  = f"[{tag}]"

    print(f"\n{'─'*60}\n{prefix} {url}\n       → {out_dir}")

    # Each chapter gets its own browser context so cookies/fingerprints are isolated
    context = await browser.new_context(
        viewport    = {"width": 1366, "height": 900},
        user_agent  = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        locale      = "en-US",
        timezone_id = "America/New_York",
    )
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
        window.chrome = { runtime: {} };
    """)
    page = await context.new_page()

    try:
        # ── Page load (bot-detection zone — keep delays here) ─────────────
        print(f"{prefix} Loading page …")
        await page.goto(url, wait_until="domcontentloaded",
                        timeout=PAGE_LOAD_TIMEOUT)
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout:
            pass  # continue anyway

        print(f"{prefix} Scrolling to trigger lazy-load …")
        for _ in range(SCROLL_STEPS):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(SCROLL_PAUSE)
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(SETTLE_WAIT)

        # ── Find image URLs ────────────────────────────────────────────────
        image_urls = await find_images(page)
        if not image_urls:
            print(f"{prefix} ✗ No images found — saving debug screenshot.")
            await page.screenshot(path=str(out_dir / "debug_screenshot.png"),
                                  full_page=True)
            return False

        # Pass browser cookies into the requests session so CDN auth works
        cookies = await context.cookies()
        session = requests.Session()
        for c in cookies:
            session.cookies.set(c["name"], c["value"],
                                domain=c.get("domain", ""))

    finally:
        await page.close()
        await context.close()

    # ── Parallel image downloads (CDN — no bot detection) ─────────────────
    print(f"{prefix} Downloading {len(image_urls)} images "
          f"({DOWNLOAD_CONCURRENCY} at a time) …")

    sem   = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
    tasks = []
    dests = []

    for idx, img_url in enumerate(image_urls, start=1):
        if img_url.startswith("//"):
            img_url = "https:" + img_url
        elif img_url.startswith("/"):
            p = urlparse(url)
            img_url = f"{p.scheme}://{p.netloc}{img_url}"

        ext  = (re.search(r"\.(jpg|jpeg|png|webp)", img_url, re.I) or
                type("m", (), {"group": lambda *_: ".jpg"})()).group(0).lower()
        dest = out_dir / f"{idx:04d}{ext}"
        dests.append((idx, len(image_urls), dest))

        if dest.exists():
            async def _already_done(): return True
            tasks.append(_already_done())
        else:
            tasks.append(download_image(img_url, dest, session, url, sem))

    results = await asyncio.gather(*tasks)

    ok = sum(1 for r in results if r)
    for (idx, total, dest), success in zip(dests, results):
        status = "✓" if success else "✗"
        print(f"  {status} [{idx:>3}/{total}] {dest.name}")

    print(f"\n{prefix} Downloaded {ok}/{len(image_urls)} images.")

    if ok > 0:
        build_pdf(out_dir)

    return ok == len(image_urls)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main(urls: list[str]) -> None:
    print(f"Starting manga scraper  ({CHAPTER_CONCURRENCY} chapters in parallel, "
          f"{DOWNLOAD_CONCURRENCY} images/chapter in parallel)")
    print(f"Output: {OUTPUT_DIR.resolve()}\n")

    urls = [u.strip() for u in urls if u.strip()]

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

        # Process chapters in capped-concurrency batches
        sem      = asyncio.Semaphore(CHAPTER_CONCURRENCY)
        results  = {}

        async def run_one(url: str, delay: float) -> None:
            async with sem:
                results[url] = await scrape_chapter(url, browser,
                                                    start_delay=delay)

        await asyncio.gather(*[
            run_one(url, i * random.uniform(*INTER_CHAPTER_DELAY))
            for i, url in enumerate(urls)
        ])

        await browser.close()

    print("\n" + "═" * 60)
    print("Summary:")
    for url, ok in results.items():
        print(f"  {'✓ OK  ' if ok else '✗ FAIL'} {url}")
    print("═" * 60)
    if not all(results.values()):
        print("\nSome chapters had issues — check the log above.")


if __name__ == "__main__":
    target_urls = (
        sys.argv[1:]
        if len(sys.argv) > 1
        else [u for u in Path(URLS_FILE).read_text().splitlines()
              if u.strip()]
        if Path(URLS_FILE).exists()
        else []
    )
    if not target_urls:
        print("No URLs to scrape. Add them to urls.txt or pass as arguments.")
        sys.exit(1)

    asyncio.run(main(target_urls))
