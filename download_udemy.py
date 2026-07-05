import json
import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright
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


def stop_playback(page):
    try:
        page.evaluate(
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


def try_start_playback(page):
    # udemy players often need a click before hls requests fire
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
            if loc.count() > 0 and loc.is_visible(timeout=1500):
                loc.click(timeout=3000)
                return
        except Exception:
            pass
    try:
        page.evaluate(
            """() => {
                const v = document.querySelector('video');
                if (v) { v.muted = true; v.play().catch(() => {}); }
            }"""
        )
    except Exception:
        pass


def wait_for_m3u8(page, capture, timeout_sec):
    deadline = time.time() + timeout_sec
    last_url = None
    while time.time() < deadline:
        if capture.url and capture.url != last_url:
            last_url = capture.url
            # brief settle window so we prefer the newest 1080p playlist
            page.wait_for_timeout(1500)
            if capture.url == last_url:
                return capture.url
        page.wait_for_timeout(400)
    return capture.url


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


def open_browser_context(playwright, profile_dir, login_only=False):
    launch_args = {
        'user_data_dir': profile_dir,
        'headless': False,
        'viewport': {'width': 1280, 'height': 800},
        'args': ['--disable-blink-features=AutomationControlled'],
    }
    for channel in ('chrome', 'msedge', None):
        try:
            if channel:
                return playwright.chromium.launch_persistent_context(channel=channel, **launch_args)
            return playwright.chromium.launch_persistent_context(**launch_args)
        except Exception:
            continue
    raise RuntimeError('could not launch browser. run: playwright install chrome')


def run_downloads(csv_path, output_dir, profile_dir, timeout_sec, start_at, limit, login_only):
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
    used_m3u8_urls = set()
    success = 0
    failed = 0
    skipped = 0
    error_log_path = os.path.join(output_dir, 'download_errors.log')

    with sync_playwright() as p:
        context = open_browser_context(p, profile_dir, login_only)
        page = context.pages[0] if context.pages else context.new_page()

        if login_only:
            page.goto('https://www.udemy.com/', wait_until='domcontentloaded')
            print("Log into Udemy in the browser window, then close it or press Ctrl+C here.")
            try:
                while context.pages:
                    page.wait_for_timeout(1000)
            except KeyboardInterrupt:
                pass
            context.close()
            return

        capture = M3u8Capture()
        page.on('request', capture.on_request)
        page.on('response', capture.on_response)

        bar = tqdm(lectures, unit='vid', dynamic_ncols=True)
        for row in bar:
            chapter = row['chapter']
            title = row['title']
            url = row['url']
            lecture_id = lecture_id_from_url(url)
            expected_asset_id = asset_map.get(lecture_id) if lecture_id else None
            chapter_dir = os.path.join(output_dir, sanitize_filename(chapter))
            output_path = os.path.join(chapter_dir, f"{sanitize_filename(title)}.mp4")

            if os.path.isfile(output_path) and os.path.getsize(output_path) > 1024:
                skipped += 1
                bar.set_postfix(ok=success, skip=skipped, fail=failed, last='skip')
                continue

            if not expected_asset_id:
                failed += 1
                with open(error_log_path, 'a', encoding='utf-8') as logf:
                    logf.write(f"{title} | {url} | no asset id mapped\n")
                bar.set_postfix(ok=success, skip=skipped, fail=failed, last='fail:no_asset')
                continue

            page_asset_hits = []

            def on_request(request):
                match = ASSET_PATH_RE.search(request.url)
                if match:
                    page_asset_hits.append(match.group(1))
                capture.on_request(request)

            page.remove_listener('request', capture.on_request)
            page.remove_listener('response', capture.on_response)
            page.on('request', on_request)
            page.on('response', capture.on_response)

            capture.reset()
            status = 'fail'
            try:
                page.goto('about:blank', wait_until='domcontentloaded', timeout=30000)
                stop_playback(page)
                page.wait_for_timeout(800)

                page_asset_hits.clear()
                page.goto(url, wait_until='domcontentloaded', timeout=60000)
                page.wait_for_timeout(2000)

                if page_asset_hits and expected_asset_id not in page_asset_hits:
                    page_asset_hits.clear()
                    capture.reset()
                    page.reload(wait_until='domcontentloaded', timeout=60000)
                    page.wait_for_timeout(2500)

                capture.begin(expected_asset_id)
                try_start_playback(page)
                m3u8_url = wait_for_m3u8(page, capture, timeout_sec)
                capture.stop()
                if not m3u8_url or not capture._matches_asset(m3u8_url):
                    raise RuntimeError(f'm3u8 asset mismatch (wanted {expected_asset_id})')
                if m3u8_url in used_m3u8_urls:
                    raise RuntimeError(f'duplicate stream detected (asset {expected_asset_id})')

                download_with_ffmpeg(m3u8_url, output_path)
                used_m3u8_urls.add(m3u8_url)
                success += 1
                status = 'ok'
            except Exception as exc:
                capture.stop()
                failed += 1
                status = 'fail'
                with open(error_log_path, 'a', encoding='utf-8') as logf:
                    logf.write(f"{title} | {url} | {exc}\n")

            page.remove_listener('request', on_request)
            page.on('request', capture.on_request)

            short = title if len(title) <= 28 else title[:25] + '...'
            bar.set_postfix(ok=success, skip=skipped, fail=failed, last=f'{status}:{short}')

        context.close()

    print(f"\nDone. ok={success} skip={skipped} fail={failed}")
    if failed:
        print(f"Errors logged to {error_log_path}")


def main():
    parser = argparse.ArgumentParser(description='Download Udemy lectures from CSV via m3u8 capture.')
    parser.add_argument('--csv', default=DEFAULT_CSV, help='input csv path')
    parser.add_argument('--output', default=DEFAULT_OUTPUT, help='output root folder')
    parser.add_argument('--profile', default=DEFAULT_PROFILE, help='browser profile folder (keeps login)')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT, help='seconds to wait for m3u8 per video')
    parser.add_argument('--start', type=int, default=1, help='1-based lecture index to start from')
    parser.add_argument('--limit', type=int, default=0, help='max lectures to process (0 = all)')
    parser.add_argument('--login-only', action='store_true', help='open browser once to log into Udemy')
    args = parser.parse_args()

    run_downloads(
        csv_path=args.csv,
        output_dir=args.output,
        profile_dir=args.profile,
        timeout_sec=args.timeout,
        start_at=args.start,
        limit=args.limit,
        login_only=args.login_only,
    )


if __name__ == '__main__':
    main()
