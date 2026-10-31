#!/usr/bin/env python3
"""Play every chapter in headless Chrome and check SunHi clips.

The full play waits for each MP3 to finish (playbackRate 8, so onended still
gates the next line). Letter keys are checked at natural start latency.
A missing file must fall back to speechSynthesis.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "audio" / "tts" / "manifest.json").read_text(encoding="utf-8"))
RATE = 8

INIT = f"""
window.__lhqTtsRate = {RATE};
window.__synthCalls = [];
(() => {{
  const wrap = () => {{
    const synth = window.speechSynthesis;
    if (!synth || synth.__lhqWrapped) return;
    synth.speak = function (utter) {{
      window.__synthCalls.push(utter && utter.text);
      window.setTimeout(() => {{ if (utter && typeof utter.onend === 'function') utter.onend(); }}, 20);
    }};
    synth.__lhqWrapped = true;
  }};
  wrap();
}})();
"""


def chrome_path() -> str:
    for candidate in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"):
        if Path(candidate).exists():
            return candidate
    raise SystemExit("Google Chrome was not found")


def make_handler():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            file_path = (ROOT / rel).resolve()
            if not str(file_path).startswith(str(ROOT.resolve())) or not file_path.is_file():
                self.send_error(404)
                return
            data = file_path.read_bytes()
            if file_path.suffix == ".html":
                ctype = "text/html; charset=utf-8"
            elif file_path.suffix == ".mp3":
                ctype = "audio/mpeg"
            elif file_path.suffix == ".json":
                ctype = "application/json"
            else:
                ctype = "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def start_server() -> tuple[ThreadingHTTPServer, int]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def mp3_duration(rel: str) -> float:
    path = ROOT / rel
    output = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        text=True,
    )
    return float(output.strip())


def launch(playwright):
    return playwright.chromium.launch(
        executable_path=chrome_path(),
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
        ],
    )


# Full play, fallbacks, and saved-progress checks are the non-birthday game.
# Pin the clock off October 31 so a machine dated on the birthday does not
# turn that playthrough into the candle flow. Birthday itself is play_birthday().
NORMAL_DATE = """
(() => {
  const fixedMs = Date.parse('2026-06-15T12:00:00');
  const NativeDate = Date;
  class FakeDate extends NativeDate {
    constructor(...args) {
      if (args.length === 0) super(fixedMs);
      else super(...args);
    }
    static now() { return fixedMs; }
    static parse(value) { return NativeDate.parse(value); }
    static UTC(...args) { return NativeDate.UTC(...args); }
  }
  window.Date = FakeDate;
})();
"""


def new_page(browser, port: int):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.add_init_script(NORMAL_DATE)
    context.add_init_script(INIT)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__lhqTtsReady === true")
    page.evaluate("() => window.__lhqPreloadDone()")
    return page, errors


def wait_quiet(page) -> None:
    for _ in range(30):
        page.wait_for_function("() => !window.__lhqTtsBusy()", timeout=40000)
        page.wait_for_timeout(180)
        if not page.evaluate("() => window.__lhqTtsBusy()"):
            return
    raise SystemExit("speech never went quiet")


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
    return page.evaluate("() => (document.querySelector('#levelTitle').textContent || '').trim()")


def press_key(page, key: str) -> float:
    return page.evaluate(
        """(key) => {
          const input = document.querySelector('#commandInput');
          input.focus();
          const t0 = performance.now();
          input.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
          input.value = (input.value || '') + key;
          input.dispatchEvent(new InputEvent('input', { bubbles: true, data: key, inputType: 'insertText' }));
          const last = window.__lhqLastPlay;
          if (!last || last.t < t0) return -1;
          return last.t - t0;
        }""",
        key,
    )


def check_pairs(log: list[dict]) -> None:
    stack: list[dict] = []
    checked = 0
    for event in log:
        kind = event["type"]
        if kind == "mp3":
            if stack:
                raise SystemExit(f"overlapped clip: {stack[-1]['text'][:40]} then {event['text'][:40]}")
            stack.append(event)
        elif kind == "end":
            if not stack or stack[-1]["text"] != event["text"]:
                raise SystemExit(f"end without matching start: {event['text'][:60]}")
            start = stack.pop()
            rel = MANIFEST["clips"].get(event["text"])
            if not rel:
                raise SystemExit(f"ended clip missing from manifest: {event['text'][:60]}")
            duration = mp3_duration(rel)
            elapsed = (event["t"] - start["t"]) / 1000
            expected = duration / RATE
            if duration >= 3:
                if abs(elapsed - expected) / expected > 0.4:
                    raise SystemExit(
                        f"timing off for {event['text'][:40]!r}: elapsed {elapsed:.2f}s expected {expected:.2f}s"
                    )
                checked += 1
        elif kind == "fallback":
            raise SystemExit(f"unexpected fallback: {event['text'][:80]}")
        elif kind == "cancel":
            stack.clear()
    if stack:
        raise SystemExit(f"unfinished clip: {stack[-1]['text'][:60]}")
    if checked < 8:
        raise SystemExit(f"only timing-checked {checked} clips")
    print(f"timing checked {checked} clips at {RATE}x")


def full_play(page, errors: list[str]) -> tuple[str, str]:
    start = page.locator("#startBtn")
    if "[ START MISSION ]" not in (start.inner_text() or ""):
        raise SystemExit("start control changed")
    missions = page.evaluate(
        """() => [...document.querySelectorAll('#chapterGrid .chapter-meta')]
          .reduce((sum, el) => sum + parseInt(el.textContent, 10), 0)"""
    )
    chapters = page.locator("#chapterGrid button").count()
    page.locator("#chapterGrid button").nth(0).click()
    page.wait_for_selector("#gameScreen:not(.hidden)")
    wait_quiet(page)
    decoded = page.evaluate("() => window.__lhqDecodedCount()")
    if decoded < 27:
        raise SystemExit(f"only decoded {decoded} clips before letter keys")
    letter_key = (command_text(page)[:1] or "b").lower()
    letter_dt = press_key(page, letter_key)
    letter_text = page.evaluate("() => window.__lhqLastPlay && window.__lhqLastPlay.text")
    if letter_dt < 0 or letter_dt > 50:
        raise SystemExit(f"letter key was not instant ({letter_dt} ms, text={letter_text})")
    if not letter_text or len(letter_text) > 4:
        raise SystemExit(f"unexpected letter clip {letter_text!r}")
    wait_quiet(page)
    page.evaluate("() => { document.querySelector('#commandInput').value = ''; }")
    page.evaluate(
        """() => {
          const input = document.querySelector('#commandInput');
          input.value = 'm';
          input.dispatchEvent(new InputEvent('input', { bubbles: true, data: 'm', inputType: 'insertText' }));
        }"""
    )
    page.wait_for_function(
        """() => window.__lhqTtsLog.some(event => event.type === 'mp3' && event.text.includes('Moon'))""",
        timeout=15000,
    )
    if page.evaluate("() => window.__lhqTtsStats.fallback") != 0:
        raise SystemExit("wrong-key discovery used speechSynthesis")
    print("wrong-key discovery played from mp3")
    wait_quiet(page)
    page.wait_for_function(
        """() => window.__lhqTtsLog.some(event => event.type === 'mp3' && (event.text.includes('가운데 줄') || event.text.includes('맨 아랫줄') || event.text.includes('맨 윗줄')))""",
        timeout=12000,
    )
    print("keyboard position hint played from mp3")
    wait_quiet(page)
    dash_dt = press_key(page, "-")
    dash_text = page.evaluate("() => window.__lhqLastPlay && window.__lhqLastPlay.text")
    if dash_text == "대시" and (dash_dt < 0 or dash_dt > 50):
        raise SystemExit(f"dash key was not instant ({dash_dt} ms, text={dash_text})")
    wait_quiet(page)
    print(f"letter {letter_text!r} in {letter_dt:.1f} ms, dash {dash_text!r} in {dash_dt:.1f} ms")
    page.uncheck("#letterSoundToggle")

    solved = 0
    finished = 0
    reward_text = ""
    while True:
        if is_done(page):
            wait_quiet(page)
            finished += 1
            if page.locator("#nextChapterBtn").is_visible():
                page.click("#nextChapterBtn")
                page.wait_for_function(
                    "() => document.querySelector('#doneBox').classList.contains('hidden')"
                )
                wait_quiet(page)
                continue
            break
        command = command_text(page)
        title = title_text(page)
        before = page.evaluate("() => window.__lhqTtsLog.length")
        page.fill("#commandInput", "")
        page.locator("#commandInput").fill(command)
        page.locator("#commandInput").press("Enter")
        solved += 1
        page.wait_for_function(
            """(prev) => {
              const done = document.querySelector('#doneBox');
              if (done && !done.classList.contains('hidden')) return true;
              const nowCmd = (document.querySelector('#commandText').textContent || '').replace(/\\s+/g, ' ').trim();
              const nowTitle = (document.querySelector('#levelTitle').textContent || '').trim();
              return nowCmd !== prev.cmd || nowTitle !== prev.title;
            }""",
            arg={"cmd": command, "title": title},
            timeout=20000,
        )
        wait_quiet(page)
        if solved == 1:
            fresh = page.evaluate("(n) => window.__lhqTtsLog.slice(n)", before)
            for event in fresh:
                if event["type"] == "mp3" and len(event["text"]) > 4:
                    reward_text = event["text"]
                    break
    done_text = page.locator("#doneText").inner_text()
    stats = page.evaluate("() => window.__lhqTtsStats")
    log = page.evaluate("() => window.__lhqTtsLog")
    print(f"play stats {stats} solved {solved}/{missions} chapters {finished}/{chapters}")
    print("done:", done_text)
    if errors:
        raise SystemExit("page errors:\n" + "\n".join(errors[:8]))
    if solved != missions or finished != chapters:
        raise SystemExit("did not finish every mission")
    if "ALL CHAPTERS DONE" not in done_text:
        raise SystemExit("missing final clear line")
    if stats["fallback"] != 0:
        fallbacks = [event["text"] for event in log if event["type"] == "fallback"]
        raise SystemExit(f"fallbacks during full play: {stats['fallback']} {fallbacks[:8]}")
    if stats["mp3"] < missions * 2:
        raise SystemExit(f"too few mp3 plays: {stats['mp3']}")
    check_pairs(log)
    if not reward_text:
        raise SystemExit("could not capture a reward line")
    return reward_text, letter_text, letter_key


def move_aside(text: str) -> tuple[Path, Path]:
    rel = MANIFEST["clips"][text]
    path = ROOT / rel
    backup = path.with_suffix(".mp3.bak")
    path.replace(backup)
    return path, backup


def restore(path: Path, backup: Path) -> None:
    if backup.exists():
        backup.replace(path)


def expect_fallback(page, text: str) -> None:
    # A missing clip is logged as fallback, then speak() may wait ~50ms after
    # cancel() before speechSynthesis.speak. Wait for both.
    page.wait_for_function(
        """(text) => {
          const logged = window.__lhqTtsLog.some(event => event.type === 'fallback' && event.text === text);
          const spoken = (window.__synthCalls || []).indexOf(text) !== -1;
          return logged && spoken;
        }""",
        arg=text,
        timeout=20000,
    )


def fallback_letter(browser, port: int, letter_text: str, letter_key: str) -> None:
    path, backup = move_aside(letter_text)
    try:
        page, errors = new_page(browser, port)
        if errors:
            raise SystemExit("page errors before letter fallback:\n" + "\n".join(errors[:8]))
        page.locator("#chapterGrid button").nth(0).click()
        page.wait_for_selector("#gameScreen:not(.hidden)")
        wait_quiet(page)
        if page.evaluate("() => window.__lhqTtsStats.fallback") != 0:
            raise SystemExit("intro fell back even though its clip exists")
        press_key(page, letter_key)
        expect_fallback(page, letter_text)
        print(f"missing letter file fell back: {letter_text}")
        page.context.close()
    finally:
        restore(path, backup)


def fallback_reward(browser, port: int, reward_text: str) -> None:
    path, backup = move_aside(reward_text)
    try:
        page, errors = new_page(browser, port)
        if errors:
            raise SystemExit("page errors before reward fallback:\n" + "\n".join(errors[:8]))
        page.locator("#chapterGrid button").nth(0).click()
        page.wait_for_selector("#gameScreen:not(.hidden)")
        wait_quiet(page)
        page.uncheck("#letterSoundToggle")
        command = command_text(page)
        title = title_text(page)
        page.locator("#commandInput").fill(command)
        page.locator("#commandInput").press("Enter")
        expect_fallback(page, reward_text)
        page.wait_for_function(
            """(prev) => {
              const nowTitle = (document.querySelector('#levelTitle').textContent || '').trim();
              return nowTitle !== prev;
            }""",
            arg=title,
            timeout=20000,
        )
        wait_quiet(page)
        if page.evaluate("() => window.__lhqTtsStats.mp3") < 2:
            raise SystemExit("next line did not play an mp3 after the reward fallback")
        print(f"missing reward fell back and the next line still played: {reward_text[:40]}")
        page.context.close()
    finally:
        restore(path, backup)


def date_mock_script(iso: str) -> str:
    return f"""
    (() => {{
      const fixedMs = Date.parse('{iso}');
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


def blow_candle(page) -> None:
    page.locator("#commandInput").focus()
    page.evaluate(
        """() => {
          const input = document.querySelector('#commandInput');
          input.dispatchEvent(new KeyboardEvent('keydown', {
            key: 'a', bubbles: true, cancelable: true
          }));
        }"""
    )


def play_birthday(browser, port: int, path: str, date_iso: str | None, expect_phrase: str) -> None:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    if date_iso:
        context.add_init_script(date_mock_script(date_iso))
    context.add_init_script(INIT)
    page = context.new_page()
    notes: list[str] = []
    page.on("console", lambda msg: notes.append(msg.text))
    page.goto(f"http://127.0.0.1:{port}{path}", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__lhqTtsReady === true")
    page.evaluate("() => window.__lhqPreloadDone()")
    boot = page.locator("#startScreen pre.boot").inner_text()
    if expect_phrase not in boot:
        context.close()
        raise SystemExit(f"birthday boot missing {expect_phrase!r}: {boot[:180]}")
    page.locator("#chapterGrid button").nth(0).click()
    page.wait_for_selector("#gameScreen:not(.hidden)")
    page.wait_for_function(
        """(phrase) => window.__lhqTtsLog.some(event => event.type === 'mp3' && event.text.includes(phrase))""",
        arg=expect_phrase,
        timeout=15000,
    )
    candles = page.evaluate(
        """() => (document.querySelector('#commandText').textContent || '').split('').filter(ch => ch === '*').length"""
    )
    if candles < 1:
        context.close()
        raise SystemExit("birthday screen did not show candles")
    for _ in range(candles):
        blow_candle(page)
        page.wait_for_timeout(300)
    deadline_pops = 0
    while deadline_pops < 20:
        pops = sum(1 for line in notes if line.startswith("birthday-pop"))
        if pops >= candles:
            break
        page.wait_for_timeout(100)
        deadline_pops += 1
    pops = sum(1 for line in notes if line.startswith("birthday-pop"))
    if pops < candles:
        context.close()
        raise SystemExit(f"expected {candles} candle pops, saw {pops}")
    page.wait_for_function(
        """() => window.__lhqTtsLog.some(event => event.type === 'end' && event.text.indexOf('초코가 노래해') === 0)""",
        timeout=20000,
    )
    for _ in range(250):
        if any(line.startswith("birthday-tts-done") for line in notes) and any(
            line.startswith("birthday-song ") for line in notes
        ):
            break
        page.wait_for_timeout(100)
    else:
        context.close()
        raise SystemExit("birthday song did not start: " + " | ".join(notes[-8:]))
    song_at = next(i for i, line in enumerate(notes) if line.startswith("birthday-song "))
    done_at = next(i for i, line in enumerate(notes) if line.startswith("birthday-tts-done"))
    end_at = next(i for i, line in enumerate(notes) if line.startswith("[lhq-tts] end 초코가 노래해"))
    if not (end_at < done_at < song_at):
        context.close()
        raise SystemExit(f"song started before the spoken line ended: end={end_at} done={done_at} song={song_at}")
    page.wait_for_function(
        """() => {
          const title = document.querySelector('#levelTitle')?.textContent || '';
          return title.includes('CH1') || title.includes('알파벳');
        }""",
        timeout=25000,
    )
    page.wait_for_function(
        """() => window.__lhqTtsLog.some(event => event.type === 'mp3' && (event.text.includes('2층버스') || event.text.includes('알파벳 놀이를 해요')))""",
        timeout=15000,
    )
    stats = page.evaluate("() => window.__lhqTtsStats")
    if stats["fallback"] != 0:
        fallbacks = page.evaluate(
            """() => window.__lhqTtsLog.filter(event => event.type === 'fallback').map(event => event.text)"""
        )
        context.close()
        raise SystemExit(f"birthday fallbacks: {fallbacks}")
    print(f"birthday {expect_phrase} candles={candles} pops={pops} mp3={stats['mp3']} fallback=0 song-after-tts")
    context.close()


def cancel_and_restart(page) -> None:
    page.locator("#chapterGrid button").nth(0).click()
    page.wait_for_function("() => window.__lhqTtsStats.mp3 >= 1", timeout=10000)
    page.click("#menuBtn")
    page.wait_for_function("() => !window.__lhqTtsBusy()", timeout=10000)
    page.wait_for_selector("#startScreen:not(.hidden)")
    played = page.evaluate("() => window.__lhqTtsStats.mp3")
    page.locator("#chapterGrid button").nth(0).click()
    page.wait_for_function(f"() => window.__lhqTtsStats.mp3 > {played}", timeout=10000)
    page.click("#menuBtn")
    page.wait_for_function("() => !window.__lhqTtsBusy()", timeout=10000)
    print("cancel and restart ok")


def old_saved_progress(browser, port: int) -> None:
    page, errors = new_page(browser, port)
    page.evaluate(
        """() => {
          localStorage.setItem('lhq_cleared_chapters', JSON.stringify(['abc1', 'abc2', 'abc3', 'cmd', 'words', 'nope']));
          localStorage.removeItem('lhq_progress_version');
        }"""
    )
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function("() => window.__lhqTtsReady === true")
    page.wait_for_selector("#chapterGrid .chapter-btn")
    state = page.evaluate(
        """() => {
          const buttons = [...document.querySelectorAll('.chapter-btn')].map(btn => ({
            text: btn.textContent.replace(/\\s+/g, ' ').trim(),
            cleared: btn.classList.contains('cleared')
          }));
          return {
            version: localStorage.getItem('lhq_progress_version'),
            cleared: JSON.parse(localStorage.getItem('lhq_cleared_chapters') || '[]'),
            buttons
          };
        }"""
    )
    if errors:
        raise SystemExit("old progress page errors:\n" + "\n".join(errors[:8]))
    if state["version"] != "2":
        raise SystemExit(f"progress version was not migrated: {state['version']}")
    for chapter_id in ("abc1", "abc2", "abc3"):
        if chapter_id in state["cleared"]:
            raise SystemExit(f"rewritten chapter stayed cleared: {chapter_id}")
    if "cmd" not in state["cleared"]:
        raise SystemExit(f"command clear was dropped: {state['cleared']}")
    abc1 = next(btn for btn in state["buttons"] if "재윤이 정류장" in btn["text"])
    cmd = next(btn for btn in state["buttons"] if "첫 명령어" in btn["text"])
    if abc1["cleared"] or not cmd["cleared"]:
        raise SystemExit(f"menu checks wrong: {abc1} {cmd}")
    print("old saved progress migrated without breaking the menu")
    page.context.close()


def main() -> None:
    from playwright.sync_api import sync_playwright

    httpd, port = start_server()
    try:
        with sync_playwright() as playwright:
            browser = launch(playwright)
            if not os.environ.get("VERIFY_PLAY_ONLY"):
                play_birthday(browser, port, "/index.html", "2026-10-31T12:00:00", "네 살")
                play_birthday(browser, port, "/index.html", "2027-10-31T12:00:00", "다섯 살")
                play_birthday(browser, port, "/index.html?birthday=1", None, "네 살")

                page, _errors = new_page(browser, port)
                cancel_and_restart(page)
                page.context.close()

                old_saved_progress(browser, port)
            page, errors = new_page(browser, port)
            reward, letter_text, letter_key = full_play(page, errors)
            page.context.close()

            fallback_letter(browser, port, letter_text, letter_key)
            fallback_reward(browser, port, reward)
            browser.close()
    finally:
        httpd.shutdown()
        for backup in (ROOT / "audio" / "tts").glob("*.mp3.bak"):
            original = backup.with_suffix("")
            # with_suffix("") on "abc.mp3.bak" -> "abc.mp3" because suffix is .bak
            if not original.exists():
                backup.replace(original)
    print("VERIFY OK")


if __name__ == "__main__":
    main()
