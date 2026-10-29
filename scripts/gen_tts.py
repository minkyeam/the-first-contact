#!/usr/bin/env python3
"""Build SunHi MP3 clips for every line the game speaks.

Usage (from the repo root):

    python3 scripts/gen_tts.py

Requires Python 3, Google Chrome, and:

    python3 -m pip install edge-tts playwright

The script opens index.html in headless Chrome, plays every chapter (including
wrong answers, repeats, letter keys, and each chapter-clear variant), and
calls in-page helpers when later missions add them (letter discoveries,
keyboard hints, birthday ages). It then writes any missing files under
audio/tts/. Existing clips are left in place, so it is safe to rerun after
other branches merge.

Voice: ko-KR-SunHiNeural. Rate: -5%. To change either, delete audio/tts/*.mp3
and audio/tts/manifest.json, then run this script again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import unicodedata
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"
AUDIO_DIR = ROOT / "audio" / "tts"
MANIFEST_PATH = AUDIO_DIR / "manifest.json"
VOICE = "ko-KR-SunHiNeural"
RATE = "-5%"
SHORT_MAX = 4
NEXT_CAP = 8

SPEAK_NEEDLE = "function speak(text, options = {}) {"
SPEAK_STUB = """function speak(text, options = {}) {
      try { if (gameStarted) window.__lhqRecord(text); } catch (_err) {}
      return Promise.resolve(true);
    }
    function speakCollectorDisabled(text, options = {}) {"""

ENUM_INJECT = r"""
    window.__lhqLetterNames = (typeof letterKo !== 'undefined')
      ? Object.keys(letterKo).map(function (key) { return letterKo[key]; }).concat(['대시'])
      : [];
    window.__lhqChapterIds = [];
    window.__lhqLevelTotal = 0;
    window.__lhqPlainLevels = 0;
    try {
      if (typeof chapters !== 'undefined') {
        window.__lhqChapterIds = chapters.map(function (chapter) { return chapter.id; });
        window.__lhqLevelTotal = chapters.reduce(function (sum, chapter) {
          return sum + ((chapter.levels && chapter.levels.length) || 0);
        }, 0);
        chapters.forEach(function (chapter) {
          (chapter.levels || []).forEach(function (level) {
            if (!level || !level.story) window.__lhqPlainLevels += 1;
          });
        });
      }
    } catch (_error) { /* ignore */ }
    window.__lhqHasBirthday = typeof agePhrase === 'function';
    window.__lhqClearedKey = (typeof CLEARED_KEY !== 'undefined') ? CLEARED_KEY : 'lhq_cleared_chapters';
    window.__lhqProgressKey = (typeof PROGRESS_VERSION_KEY !== 'undefined') ? PROGRESS_VERSION_KEY : '';
    window.__lhqDebugLevel = function () {
      try {
        const level = chapters[currentChapter].levels[currentLevel];
        return {
          story: !!(level && level.story),
          guide: !!(level && level.guide)
        };
      } catch (_error) {
        return { story: false, guide: false };
      }
    };
    if (typeof window.__lhqNormalize !== 'function') {
      window.__lhqNormalize = function (text) {
        return String(text == null ? '' : text).normalize('NFC').replace(/\s+/g, ' ').trim();
      };
    }
    window.__lhqEnumerate = function () {
      const lines = [];
      const add = function (value) {
        if (typeof value === 'string' && value.trim()) lines.push(value);
      };
      let letters = [];
      try {
        if (typeof letterKo !== 'undefined') letters = Object.keys(letterKo);
      } catch (_error) { letters = []; }
      letters.forEach(function (letter) { add(letterKo[letter]); });
      add('대시');
      try {
        const input = document.getElementById('commandInput');
        if (input) input.value = '';
        if (typeof chapters !== 'undefined' && typeof buildIntroTTS === 'function') {
          chapters.forEach(function (chapter) {
            (chapter.levels || []).forEach(function (level) {
              let intro = '';
              try { intro = buildIntroTTS(level); } catch (_error) { intro = ''; }
              add(intro);
              if (chapter.introTTS && intro) add(chapter.introTTS + ' ' + intro);
              if (level && typeof level.rewardTTS === 'string') add(level.rewardTTS);
              if (level && level.story && typeof discoverySpeech === 'function') {
                const expected = String(level.command || '').replace(/\s+/g, '').toUpperCase();
                expected.split('').forEach(function (target) {
                  if (!letterKo[target]) return;
                  letters.forEach(function (wrong) {
                    try { add(discoverySpeech(level, wrong, target, false)); } catch (_error) {}
                    try { add(discoverySpeech(level, wrong, target, true)); } catch (_error) {}
                  });
                  add('어, 글자가 아니야. 다음은 ' + letterKo[target] + '.');
                  if (typeof nonLetterDiscovery === 'function') {
                    try { add(nonLetterDiscovery(level, target, false)); } catch (_error) {}
                    try { add(nonLetterDiscovery(level, target, true)); } catch (_error) {}
                  }
                });
              }
            });
          });
        }
      } catch (_error) { /* ignore */ }
      if (typeof positionHint === 'function') {
        letters.forEach(function (letter) {
          try { add(positionHint(letter)); } catch (_error) {}
        });
        add('좋아. 이제 엔터!');
      }
      return lines;
    };
"""


def normalize_speech(text: str) -> str:
    text = unicodedata.normalize("NFC", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clip_filename(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{digest}.mp3"


def instrument_html(html: str) -> str:
    count = html.count(SPEAK_NEEDLE)
    if count != 1:
        raise SystemExit(f"expected 1 speak() definition, found {count}")
    html = html.replace(SPEAK_NEEDLE, SPEAK_STUB, 1)
    marker = "</script>"
    index = html.rfind(marker)
    if index < 0:
        raise SystemExit("index.html has no script block")
    return html[:index] + ENUM_INJECT + "\n  " + html[index:]


def make_handler(body: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path not in ("/", "/index.html"):
                self.send_error(404)
                return
            data = body
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def start_server(body: bytes) -> tuple[ThreadingHTTPServer, int]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(body))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


def chrome_path() -> str:
    for candidate in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"):
        if Path(candidate).exists():
            return candidate
    raise SystemExit("Google Chrome was not found")


def longs(lines: list[str]) -> list[str]:
    return [line for line in lines if len(line) > SHORT_MAX]


class Collector:
    def __init__(self) -> None:
        self.raw_lines: list[str] = []
        self.lines: list[str] = []
        self.preload: set[str] = set()
        self.prime: list[str] = []
        self.next_map: dict[str, list[str]] = defaultdict(list)

    def absorb(self, raw_items: list[str]) -> list[str]:
        norms: list[str] = []
        for raw in raw_items:
            self.raw_lines.append(raw)
            norm = normalize_speech(raw)
            if not norm:
                continue
            self.lines.append(norm)
            if len(norm) <= SHORT_MAX:
                self.preload.add(norm)
            norms.append(norm)
        return norms

    def link(self, sources: list[str], targets: list[str]) -> None:
        for source in sources:
            if len(source) <= SHORT_MAX:
                continue
            bucket = self.next_map[source]
            for target in targets:
                if not target or target == source or len(target) <= SHORT_MAX:
                    continue
                if target in bucket:
                    continue
                if len(bucket) >= NEXT_CAP:
                    break
                bucket.append(target)


def drain(page, collector: Collector) -> list[str]:
    raw = page.evaluate(
        """() => {
          const lines = (window.__lhqLines || []).slice();
          window.__lhqLines.length = 0;
          return lines;
        }"""
    )
    return collector.absorb(raw)


def is_done(page) -> bool:
    return page.evaluate(
        """() => {
          const el = document.querySelector('#doneBox');
          return !!el && !el.classList.contains('hidden');
        }"""
    )


def command_text(page) -> str:
    return page.evaluate(
        """() => (document.querySelector('#commandText').textContent || '').replace(/\\s+/g, ' ').trim()"""
    )


def title_text(page) -> str:
    return page.evaluate(
        """() => (document.querySelector('#levelTitle').textContent || '').trim()"""
    )


def sweep_letters(page) -> None:
    chars = list("abcdefghijklmnopqrstuvwxyz") + ["-", "—", "–"]
    for ch in chars:
        page.evaluate(
            """(ch) => {
              const input = document.querySelector('#commandInput');
              input.dispatchEvent(new KeyboardEvent('keydown', { key: ch, bubbles: true }));
              input.value += ch;
              input.dispatchEvent(new InputEvent('input', { bubbles: true, data: ch, inputType: 'insertText' }));
            }""",
            ch,
        )
        page.wait_for_timeout(90)
    page.fill("#commandInput", "")


def play_level(page, collector: Collector, pending: list[str], record_links: bool, do_sweep: bool) -> tuple[list[str], bool]:
    if is_done(page):
        return pending, True
    intro = drain(page, collector)
    if do_sweep:
        sweep_letters(page)
        collector.link(longs(intro), longs(drain(page, collector)))
    page.click("#repeatBtn")
    repeated = drain(page, collector)
    page.fill("#commandInput", "zzz")
    page.locator("#commandInput").press("Enter")
    error = drain(page, collector)
    command = command_text(page)
    title = title_text(page)
    if not command:
        raise RuntimeError("level has an empty command")
    page.fill("#commandInput", "")
    page.locator("#commandInput").press_sequentially(command, delay=10)
    page.click("#repeatBtn")
    midway = drain(page, collector)
    page.locator("#commandInput").press("Enter")
    page.wait_for_function(
        """(prev) => {
          const done = document.querySelector('#doneBox');
          if (done && !done.classList.contains('hidden')) return true;
          const nowCmd = (document.querySelector('#commandText').textContent || '').replace(/\\s+/g, ' ').trim();
          const nowTitle = (document.querySelector('#levelTitle').textContent || '').trim();
          return nowCmd !== prev.cmd || nowTitle !== prev.title;
        }""",
        arg={"cmd": command, "title": title},
        timeout=10000,
    )
    after = drain(page, collector)
    if record_links:
        collector.link(longs(pending), longs(intro))
        collector.link(longs(intro), longs(repeated) + longs(error) + longs(midway) + longs(after))
        collector.link(longs(repeated), longs(error) + longs(after))
        collector.link(longs(error), longs(after))
        collector.link(longs(after), longs(after[1:]))
    return (longs(after) or pending), is_done(page)


def open_chapter(page, index: int) -> None:
    page.locator("#chapterGrid button").nth(index).click()
    page.wait_for_selector("#gameScreen:not(.hidden)", timeout=10000)


def finish_open_chapter(page, collector: Collector, record_links: bool, sweep: bool, take_prime: bool) -> None:
    marker = len(collector.lines)
    pending: list[str] = []
    for step in range(40):
        if is_done(page):
            break
        pending, done = play_level(page, collector, pending, record_links, sweep and step == 0)
        if take_prime and step == 0:
            for line in collector.lines[marker:]:
                if len(line) > SHORT_MAX and line not in collector.prime:
                    collector.prime.append(line)
                    break
        if done:
            return
    else:
        raise RuntimeError(f"stuck on {title_text(page)} / {command_text(page)}")
    if not is_done(page):
        raise RuntimeError(f"chapter stopped early on {title_text(page)} / {command_text(page)}")


def play_from_menu(page, collector: Collector, index: int, record_links: bool, sweep: bool, take_prime: bool) -> None:
    open_chapter(page, index)
    try:
        finish_open_chapter(page, collector, record_links, sweep, take_prime)
    except Exception:
        page.screenshot(path="/tmp/lhq-collect-fail.png")
        status = page.evaluate("() => document.querySelector('#status')?.textContent || ''")
        print("COLLECT FAIL", title_text(page), command_text(page), status, flush=True)
        raise


def set_cleared(page, ids: list[str]) -> None:
    page.evaluate(
        """(ids) => {
          const key = window.__lhqClearedKey || 'lhq_cleared_chapters';
          localStorage.setItem(key, JSON.stringify(ids));
          if (window.__lhqProgressKey) localStorage.setItem(window.__lhqProgressKey, '99');
        }""",
        ids,
    )


def collect_birthday(browser, port: int, collector: Collector) -> None:
    for age in range(1, 21):
        year = 2022 + age
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.add_init_script(
            f"""
            (() => {{
              const fixedMs = Date.parse('{year}-10-31T12:00:00');
              const NativeDate = Date;
              class FakeDate extends NativeDate {{
                constructor(...args) {{
                  if (args.length === 0) super(fixedMs);
                  else super(...args);
                }}
                static now() {{ return fixedMs; }}
                static parse(value) {{ return NativeDate.parse(value); }}
                static UTC(...args) {{ return NativeDate.UTC(...args); }}
              }}
              window.Date = FakeDate;
            }})();
            """
        )
        context.add_init_script(
            """
            window.__lhqLines = [];
            window.__lhqRecord = function (text) {
              const value = String(text == null ? '' : text);
              if (!value.trim()) return;
              window.__lhqLines.push(value);
            };
            """
        )
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{port}/index.html?birthday=1", wait_until="domcontentloaded")
        got_year = page.evaluate("() => new Date().getFullYear()")
        if got_year != year:
            context.close()
            raise SystemExit(f"birthday date mock failed: expected {year}, got {got_year}")
        page.locator("#chapterGrid button").nth(0).click()
        page.wait_for_selector("#gameScreen:not(.hidden)")
        page.click("#repeatBtn")
        for _ in range(6):
            page.locator("#commandInput").focus()
            page.evaluate(
                """() => {
                  const input = document.querySelector('#commandInput');
                  input.dispatchEvent(new KeyboardEvent('keydown', {
                    key: 'a', bubbles: true, cancelable: true
                  }));
                }"""
            )
            page.wait_for_timeout(280)
        page.wait_for_timeout(150)
        drain(page, collector)
        context.close()
        print(f"birthday age {age}", flush=True)


def collect_lines() -> dict:
    from playwright.sync_api import sync_playwright

    html = instrument_html(INDEX.read_text(encoding="utf-8"))
    httpd, port = start_server(html.encode("utf-8"))
    collector = Collector()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome_path(),
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            context.add_init_script(
                """
                window.__lhqLines = [];
                window.__lhqRecord = function (text) {
                  const value = String(text == null ? '' : text);
                  if (!value.trim()) return;
                  window.__lhqLines.push(value);
                };
                """
            )
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="domcontentloaded")
            page.wait_for_selector("#chapterGrid button")
            ids = page.evaluate("() => window.__lhqChapterIds")
            level_total = page.evaluate("() => window.__lhqLevelTotal")
            button_count = page.locator("#chapterGrid button").count()
            if not ids or button_count != len(ids):
                raise SystemExit(f"chapter menu mismatch: buttons={button_count} ids={ids}")
            print(f"collecting {len(ids)} chapters / {level_total} levels", flush=True)

            # Sequential play records the success path, wrong-answer lines, and repeats.
            open_chapter(page, 0)
            finish_open_chapter(page, collector, True, True, True)
            for index in range(1, len(ids)):
                if page.locator("#nextChapterBtn").is_visible():
                    page.click("#nextChapterBtn")
                    page.wait_for_function(
                        "() => document.querySelector('#doneBox').classList.contains('hidden')",
                        timeout=10000,
                    )
                else:
                    page.click("#menuBtn")
                    page.wait_for_selector("#startScreen:not(.hidden)")
                    open_chapter(page, index)
                finish_open_chapter(page, collector, True, False, True)
                print(f"sequential chapter {index + 1}/{len(ids)}", flush=True)

            # Last chapter alone: the clear line without "next chapter" or "all clear".
            page.evaluate("() => localStorage.clear()")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("#chapterGrid button")
            play_from_menu(page, collector, len(ids) - 1, False, False, False)
            print("solo last chapter", flush=True)

            # Each chapter as the final cleared one, so every all-clear sentence exists.
            for index, chapter_id in enumerate(ids):
                others = [item for item in ids if item != chapter_id]
                set_cleared(page, others)
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector("#chapterGrid button")
                play_from_menu(page, collector, index, False, False, False)
                print(f"all-clear variant {index + 1}/{len(ids)}", flush=True)

            extra = page.evaluate("() => window.__lhqEnumerate()")
            collector.absorb(extra)
            letter_names = page.evaluate("() => window.__lhqLetterNames")
            has_birthday = page.evaluate("() => window.__lhqHasBirthday")
            plain_levels = page.evaluate("() => window.__lhqPlainLevels || window.__lhqLevelTotal")

            if has_birthday:
                print("collecting birthday ages 1-20", flush=True)
                collect_birthday(browser, port, collector)

            # Compare Python and JS normalization on every raw string.
            unique_raw = list(dict.fromkeys(collector.raw_lines))
            for start in range(0, len(unique_raw), 400):
                chunk = unique_raw[start:start + 400]
                js_norms = page.evaluate("(texts) => texts.map(text => window.__lhqNormalize(text))", chunk)
                for raw, js_norm in zip(chunk, js_norms):
                    py_norm = normalize_speech(raw)
                    if py_norm != js_norm:
                        raise SystemExit(f"normalize mismatch\npython: {py_norm!r}\njs: {js_norm!r}")

            context.close()
            browser.close()
            if errors:
                raise SystemExit("page errors during collection:\n" + "\n".join(errors[:12]))
    finally:
        httpd.shutdown()

    missing_letters = [name for name in letter_names if name not in collector.lines]
    if missing_letters:
        raise SystemExit(f"missing letter sounds: {missing_letters}")
    unique = list(dict.fromkeys(collector.lines))
    if level_total and len(unique) < level_total * 2:
        raise SystemExit(f"only collected {len(unique)} lines for {level_total} levels")
    source = INDEX.read_text(encoding="utf-8")
    if "function agePhrase" in source:
        age_words = [
            "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉", "열",
            "열한", "열두", "열세", "열네", "열다섯", "열여섯", "열일곱", "열여덟", "열아홉", "스무",
        ]
        missing_ages = [word for word in age_words if not any(f"{word} 살" in line for line in unique)]
        if missing_ages:
            raise SystemExit(f"missing birthday ages: {missing_ages}")
        for required in ("후!", "초코가 노래해! 생일 축하해 재윤아!"):
            if required not in unique:
                raise SystemExit(f"missing birthday line: {required}")
    if "괜찮아, 다시 해보자" in source:
        errors_found = [line for line in unique if line.startswith("괜찮아")]
        # Story missions speak a discovery line instead of this retry.
        retry_need = plain_levels if "function discoverySpeech" in source else level_total
        if len(errors_found) < retry_need:
            raise SystemExit(f"expected {retry_need} retry lines, found {len(errors_found)}")
    if "성공!" in source:
        successes = [line for line in unique if "성공" in line]
        clear_need = len(ids) + 1
        if "function discoverySpeech" in source:
            # Story chapters thank Choco instead of saying 성공, and the
            # all-clear line says 해냈어요.
            if not any("해냈어요" in line for line in unique):
                raise SystemExit("missing all-clear line")
            clear_need = 1
        if len(successes) < clear_need:
            raise SystemExit(f"expected chapter clear lines, found {len(successes)}")

    next_out = {}
    for source_line, targets in collector.next_map.items():
        if source_line not in unique:
            continue
        kept = [target for target in targets if target in unique and target != source_line]
        if kept:
            next_out[source_line] = kept[:NEXT_CAP]
    prime = [line for line in collector.prime if line in unique and len(line) > SHORT_MAX]
    preload = sorted(line for line in collector.preload if line in unique)
    print(f"collected {len(unique)} unique lines ({len(preload)} short)", flush=True)
    return {
        "lines": unique,
        "preload": preload,
        "prime": prime,
        "next": next_out,
    }


def valid_mp3(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 400:
        return False
    header = path.read_bytes()[:3]
    if header.startswith(b"ID3"):
        return True
    return len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0


def reset_needed() -> bool:
    if not MANIFEST_PATH.exists():
        return False
    try:
        meta = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return True
    return meta.get("voice") != VOICE or meta.get("rate") != RATE


async def synthesize_all(lines: list[str]) -> None:
    import asyncio
    import edge_tts

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    if reset_needed():
        print("voice or rate changed; regenerating every clip", flush=True)
        for path in AUDIO_DIR.glob("*.mp3"):
            path.unlink()

    pending = []
    skipped = 0
    for line in lines:
        path = AUDIO_DIR / clip_filename(line)
        if valid_mp3(path):
            skipped += 1
            continue
        pending.append((line, path))
    print(f"skip {skipped} existing, synthesize {len(pending)}", flush=True)

    semaphore = asyncio.Semaphore(4)
    failures: list[str] = []
    done = 0

    async def one(text: str, path: Path) -> None:
        nonlocal done
        tmp = path.with_suffix(".part")
        async with semaphore:
            last_error = "unknown"
            for attempt in range(6):
                try:
                    communicate = edge_tts.Communicate(text, VOICE, rate=RATE)
                    await communicate.save(str(tmp))
                    if not valid_mp3(tmp):
                        raise RuntimeError("edge-tts wrote an invalid mp3")
                    tmp.replace(path)
                    done += 1
                    if done == len(pending) or done % 10 == 0:
                        print(f"generated {done}/{len(pending)}", flush=True)
                    return
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
                    if tmp.exists():
                        tmp.unlink()
                    await asyncio.sleep(1.2 * (attempt + 1))
            failures.append(f"{text[:40]}: {last_error}")

    if pending:
        await asyncio.gather(*(one(text, path) for text, path in pending))
    if failures:
        raise SystemExit("TTS failed for:\n" + "\n".join(failures[:12]))


def write_manifest(collected: dict) -> tuple[int, int]:
    clips = {}
    seen: dict[str, str] = {}
    for line in collected["lines"]:
        name = clip_filename(line)
        previous = seen.get(name)
        if previous is not None and previous != line:
            raise SystemExit(f"hash collision between {previous!r} and {line!r}")
        seen[name] = line
        path = AUDIO_DIR / name
        if not valid_mp3(path):
            raise SystemExit(f"missing clip for {line!r}")
        clips[line] = f"audio/tts/{name}"

    manifest = {
        "voice": VOICE,
        "rate": RATE,
        "format": "audio/mpeg",
        "channels": 1,
        "sampleRate": 24000,
        "clips": clips,
        "preload": collected["preload"],
        "prime": collected["prime"],
        "next": collected["next"],
    }
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    referenced = {Path(rel).name for rel in clips.values()}
    for path in AUDIO_DIR.iterdir():
        if path.name == "manifest.json":
            continue
        if path.suffix == ".part" or (path.suffix == ".mp3" and path.name not in referenced):
            path.unlink()
    total = sum((AUDIO_DIR / name).stat().st_size for name in referenced)
    return len(referenced), total


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate SunHi MP3 clips for the game")
    parser.add_argument("--collect-only", action="store_true", help="print the collected line count and exit")
    args = parser.parse_args()
    collected = collect_lines()
    if args.collect_only:
        print(f"COLLECTED {len(collected['lines'])}")
        return
    import asyncio
    asyncio.run(synthesize_all(collected["lines"]))
    count, total = write_manifest(collected)
    print(f"CLIPS {count}")
    print(f"BYTES {total}")
    print(f"wrote {MANIFEST_PATH.relative_to(ROOT)} ({count} clips, {total / (1024 * 1024):.2f} MiB)")


if __name__ == "__main__":
    main()
