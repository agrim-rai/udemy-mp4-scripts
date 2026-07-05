import json
import argparse
import asyncio
import csv
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from playwright.async_api import async_playwright
from tqdm import tqdm

REFERER = "https://www.udemy.com/"
DEFAULT_CSV = "udemy_course_links.csv"
DEFAULT_OUTPUT = "output"
DEFAULT_PROFILE = "browser_profile"
DEFAULT_TIMEOUT = 90
JSON_FILES = ['barijson0.json', 'barijsonMID.json', 'barijson1.json']
ASSET_PATH_RE = re.compile(r'/assets/(\d+)/files/')
LECTURE_ID_RE = re.compile(r'/lecture/(\d+)')


def sanitize_filename(text, max_len=180):
    name = text.strip()
    name = re.sub(r'[\s:/\\|]+', '_', name)
    name = re.sub(r'[^\w.\-]+', '_', name)
    name = re.sub(r'_+', '_', name).strip('_')
    if len(name) > max_len:
        name = name[:max_len].rstrip('_')
    return name or "untitled"


def load_lectures(csv_path):
    rows = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            chapter = row.get('Chapter', '').strip()
            url = row.get('URL', '').strip()
            title = row.get('Title', '').strip()
            if chapter and url and title:
                rows.append({'chapter': chapter, 'url': url, 'title': title})
    return rows


def check_ffmpeg():
    if shutil.which('ffmpeg') is None:
        print("ffmpeg not found in PATH. Install ffmpeg and try again.")
        sys.exit(1)


def lecture_id_from_url(url):
    match = LECTURE_ID_RE.search(url)
    return match.group(1) if match else None


def load_lecture_asset_map(json_files=None):
    mapping = {}
    for file_name in json_files or JSON_FILES:
        if not os.path.exists(file_name):
            continue
        with open(file_name, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for item in data.get('results', []):
            if item.get('_class') != 'lecture':
                continue
            asset = item.get('asset') or {}
            asset_id = asset.get('id')
            lecture_id = item.get('id')
            if lecture_id and asset_id:
                mapping[str(lecture_id)] = str(asset_id)
    return mapping


def default_capture_workers():
    # ~300-600mb per tab; 8 tabs fits comfortably with 16gb+ free ram
    threads = os.cpu_count() or 6
    return min(10, max(6, threads // 2 + 2))


def default_download_workers():
    # ffmpeg -c copy is mostly network/io bound, not cpu encoding
    threads = os.cpu_count() or 6
    return min(32, max(12, threads * 2))


class M3u8Capture:
    def __init__(self):
        self.url = None
        self.active = False
        self.expected_asset_id = None

    def reset(self):
        self.url = None
        self.active = False
        self.expected_asset_id = None

    def begin(self, expected_asset_id):
        self.url = None
        self.active = True
        self.expected_asset_id = str(expected_asset_id)

    def stop(self):
        self.active = False

    def _matches_asset(self, url):
        if not self.expected_asset_id:
            return False
        return f'/assets/{self.expected_asset_id}/' in url

    def _maybe_capture(self, url):
        if not self.active:
            return
        lower = url.lower()
        if '.m3u8' not in lower or '1920x1080' not in url:
            return
        if not self._matches_asset(url):
            return
        self.url = url

    def on_request(self, request):
        self._maybe_capture(request.url)

    def on_response(self, response):
        self._maybe_capture(response.url)


class UsedM3u8Registry:
    def __init__(self):
        self._urls = set()
        self._lock = asyncio.Lock()

    async def claim(self, url):
        async with self._lock:
            if url in self._urls:
                return False
            self._urls.add(url)
            return True

    async def release(self, url):
        async with self._lock:
            self._urls.discard(url)


class PagePool:
    def __init__(self, pages):
        self._queue = asyncio.Queue()
        for page in pages:
            self._queue.put_nowait(page)

    async def acquire(self):
        return await self._queue.get()

    async def release(self, page):
        await self._queue.put(page)


class Stats:
    def __init__(self):
        self.ok = 0
        self.fail = 0
        self.skip = 0
        self.lock = asyncio.Lock()
        self.ok_titles = []
        self.fail_titles = []

    async def add_ok(self, title):
        async with self.lock:
            self.ok += 1
            self.ok_titles.append(title)

    async def add_fail(self, title):
        async with self.lock:
            self.fail += 1
            self.fail_titles.append(title)

    async def add_skip(self):
        async with self.lock:
            self.skip += 1


def download_with_ffmpeg(m3u8_url, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    cmd = [
        'ffmpeg', '-y',
        '-loglevel', 'error',
        '-headers', f'Referer: {REFERER}\r\n',
        '-i', m3u8_url,
        '-c', 'copy',
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or 'ffmpeg failed').strip()
        raise RuntimeError(err[:300])
    if not os.path.isfile(output_path) or os.path.getsize(output_path) < 1024:
        raise RuntimeError('output file missing or too small')


async def stop_playback(page):
    try:
        await page.evaluate(
            """() => {
                document.querySelectorAll('video,audio').forEach(el => {
                    el.pause();
                    el.removeAttribute('src');
                    el.load();
                });
            }"""
        )
    except Exception:
        pass


async def try_start_playback(page):
    selectors = [
        'button[data-purpose="play-button"]',
        'button[aria-label*="Play"]',
        'button[aria-label*="play"]',
        '.vjs-big-play-button',
        '[data-purpose="video-player"] button',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible(timeout=1500):
                await loc.click(timeout=3000)
                return
        except Exception:
            pass
    try:
        await page.evaluate(
            """() => {
                const v = document.querySelector('video');
                if (v) { v.muted = true; v.play().catch(() => {}); }
            }"""
        )
    except Exception:
        pass


async def wait_for_m3u8(page, capture, timeout_sec):
    deadline = time.time() + timeout_sec
    last_url = None
    while time.time() < deadline:
        if capture.url and capture.url != last_url:
            last_url = capture.url
            await page.wait_for_timeout(1500)
            if capture.url == last_url:
                return capture.url
        await page.wait_for_timeout(400)
    return capture.url


async def capture_m3u8(page, lecture_url, timeout_sec, asset_map):
    lecture_id = lecture_id_from_url(lecture_url)
    expected_asset_id = asset_map.get(lecture_id) if lecture_id else None
    if not expected_asset_id:
        raise RuntimeError(f'no asset id mapped for lecture {lecture_id}')

    capture = M3u8Capture()
    page_asset_hits = []

    def on_request(request):
        match = ASSET_PATH_RE.search(request.url)
        if match:
            page_asset_hits.append(match.group(1))
        capture.on_request(request)

    def on_response(response):
        capture.on_response(response)

    page.on('request', on_request)
    page.on('response', on_response)
    try:
        await page.goto('about:blank', wait_until='domcontentloaded', timeout=30000)
        await stop_playback(page)
        await page.wait_for_timeout(800)

        page_asset_hits.clear()
        await page.goto(lecture_url, wait_until='domcontentloaded', timeout=60000)
        await page.wait_for_timeout(2000)

        if page_asset_hits and expected_asset_id not in page_asset_hits:
            page_asset_hits.clear()
            capture.reset()
            await page.reload(wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_timeout(2500)

        capture.begin(expected_asset_id)
        await try_start_playback(page)
        m3u8_url = await wait_for_m3u8(page, capture, timeout_sec)
        capture.stop()

        if not m3u8_url:
            raise RuntimeError('no 1920x1080 m3u8 found for this lecture')
        if not capture._matches_asset(m3u8_url):
            raise RuntimeError(f'm3u8 asset mismatch (wanted {expected_asset_id})')
        return m3u8_url, expected_asset_id
    finally:
        capture.stop()
        page.remove_listener('request', on_request)
        page.remove_listener('response', on_response)


async def open_browser_context(playwright, profile_dir):
    launch_args = {
        'user_data_dir': profile_dir,
        'headless': False,
        'viewport': {'width': 1280, 'height': 800},
        'args': ['--disable-blink-features=AutomationControlled'],
    }
    for channel in ('chrome', 'msedge', None):
        try:
            if channel:
                return await playwright.chromium.launch_persistent_context(channel=channel, **launch_args)
            return await playwright.chromium.launch_persistent_context(**launch_args)
        except Exception:
            continue
    raise RuntimeError('could not launch browser. run: playwright install chrome')


async def process_lecture(
    row,
    output_dir,
    page_pool,
    executor,
    capture_sem,
    download_sem,
    timeout_sec,
    stats,
    bar,
    file_lock,
    error_log_path,
    success_log_path,
    loop,
    asset_map,
    used_m3u8,
):
    chapter = row['chapter']
    title = row['title']
    url = row['url']
    chapter_dir = os.path.join(output_dir, sanitize_filename(chapter))
    output_path = os.path.join(chapter_dir, f"{sanitize_filename(title)}.mp4")

    if os.path.isfile(output_path) and os.path.getsize(output_path) > 1024:
        await stats.add_skip()
        bar.update(1)
        bar.set_postfix(ok=stats.ok, skip=stats.skip, fail=stats.fail, last='skip')
        return

    status = 'fail'
    err_msg = ''
    m3u8_url = None
    try:
        async with capture_sem:
            page = await page_pool.acquire()
            try:
                m3u8_url, asset_id = await capture_m3u8(page, url, timeout_sec, asset_map)
            finally:
                await page_pool.release(page)

        if not await used_m3u8.claim(m3u8_url):
            raise RuntimeError(f'duplicate stream detected (asset {asset_id})')

        try:
            async with download_sem:
                await loop.run_in_executor(executor, download_with_ffmpeg, m3u8_url, output_path)
        except Exception:
            await used_m3u8.release(m3u8_url)
            raise

        await stats.add_ok(title)
        status = 'ok'
        with file_lock:
            with open(success_log_path, 'a', encoding='utf-8') as logf:
                logf.write(f"{title} | asset:{asset_id} | {output_path}\n")
    except Exception as exc:
        err_msg = str(exc)
        await stats.add_fail(title)
        with file_lock:
            with open(error_log_path, 'a', encoding='utf-8') as logf:
                logf.write(f"{title} | {url} | {err_msg}\n")

    short = title if len(title) <= 24 else title[:21] + '...'
    bar.update(1)
    bar.set_postfix(ok=stats.ok, skip=stats.skip, fail=stats.fail, last=f'{status}:{short}')


async def run_downloads_async(
    csv_path,
    output_dir,
    profile_dir,
    timeout_sec,
    start_at,
    limit,
    login_only,
    capture_workers,
    download_workers,
):
    check_ffmpeg()
    lectures = load_lectures(csv_path)
    if not lectures:
        print(f"No lectures found in {csv_path}")
        sys.exit(1)

    if start_at > 1:
        lectures = lectures[start_at - 1:]
    if limit > 0:
        lectures = lectures[:limit]

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(profile_dir, exist_ok=True)

    asset_map = load_lecture_asset_map()
    if not asset_map:
        print("Warning: lecture asset map empty. Ensure barijson*.json files are present.")

    error_log_path = os.path.join(output_dir, 'download_errors.log')
    success_log_path = os.path.join(output_dir, 'download_success.log')
    file_lock = threading.Lock()
    used_m3u8 = UsedM3u8Registry()

    async with async_playwright() as p:
        context = await open_browser_context(p, profile_dir)

        if login_only:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto('https://www.udemy.com/', wait_until='domcontentloaded')
            print("Log into Udemy in the browser window, then close it or press Ctrl+C here.")
            try:
                while context.pages:
                    await page.wait_for_timeout(1000)
            except KeyboardInterrupt:
                pass
            await context.close()
            return

        pages = list(context.pages)
        while len(pages) < capture_workers:
            pages.append(await context.new_page())
        pages = pages[:capture_workers]
        page_pool = PagePool(pages)

        stats = Stats()
        capture_sem = asyncio.Semaphore(capture_workers)
        download_sem = asyncio.Semaphore(download_workers)
        loop = asyncio.get_running_loop()

        print(
            f"hardware: {os.cpu_count()} threads | "
            f"asset map: {len(asset_map)} lectures | "
            f"capture workers: {capture_workers} | download workers: {download_workers} | "
            f"total: {len(lectures)}"
        )

        with ThreadPoolExecutor(max_workers=download_workers) as executor:
            bar = tqdm(total=len(lectures), unit='vid', dynamic_ncols=True)
            tasks = [
                process_lecture(
                    row,
                    output_dir,
                    page_pool,
                    executor,
                    capture_sem,
                    download_sem,
                    timeout_sec,
                    stats,
                    bar,
                    file_lock,
                    error_log_path,
                    success_log_path,
                    loop,
                    asset_map,
                    used_m3u8,
                )
                for row in lectures
            ]
            await asyncio.gather(*tasks)
            bar.close()

        await context.close()

    print(f"\nDone. ok={stats.ok} skip={stats.skip} fail={stats.fail}")
    if stats.ok:
        print(f"Success log: {success_log_path}")
    if stats.fail:
        print(f"Error log: {error_log_path}")
        print("Failed:")
        for t in stats.fail_titles:
            print(f"  - {t}")


def main():
    parser = argparse.ArgumentParser(description='Concurrent Udemy downloader (parallel capture + ffmpeg).')
    parser.add_argument('--csv', default=DEFAULT_CSV, help='input csv path')
    parser.add_argument('--output', default=DEFAULT_OUTPUT, help='output root folder')
    parser.add_argument('--profile', default=DEFAULT_PROFILE, help='browser profile folder (keeps login)')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT, help='seconds to wait for m3u8 per video')
    parser.add_argument('--start', type=int, default=1, help='1-based lecture index to start from')
    parser.add_argument('--limit', type=int, default=0, help='max lectures to process (0 = all)')
    parser.add_argument('--capture-workers', type=int, default=default_capture_workers(), help='parallel browser tabs for m3u8 capture')
    parser.add_argument('--download-workers', type=int, default=default_download_workers(), help='parallel ffmpeg downloads')
    parser.add_argument('--login-only', action='store_true', help='open browser once to log into Udemy')
    args = parser.parse_args()

    if args.capture_workers < 1:
        parser.error('--capture-workers must be >= 1')
    if args.download_workers < 1:
        parser.error('--download-workers must be >= 1')

    asyncio.run(
        run_downloads_async(
            csv_path=args.csv,
            output_dir=args.output,
            profile_dir=args.profile,
            timeout_sec=args.timeout,
            start_at=args.start,
            limit=args.limit,
            login_only=args.login_only,
            capture_workers=args.capture_workers,
            download_workers=args.download_workers,
        )
    )


if __name__ == '__main__':
    main()
