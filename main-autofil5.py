from __future__ import annotations

import os
import random
import re
import string
import time
import threading
from typing import Callable

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Frame, Page, TimeoutError as PWTimeoutError, Error as PWError
from playwright.sync_api import sync_playwright

# --- timings/logging helpers ---
T0 = time.perf_counter()


def _now_ms() -> int:
    return int((time.perf_counter() - T0) * 1000)


def logt(tag: str):
    print(f"[t+{_now_ms():>7}ms] {tag}")


START_URL = "https://www.infotech-s.co.jp/e-FESTA_login.html"
VIEWER_FRAME_NAMES = ("contents", "right", "ctrl", "qlist")
MAX_Q_FALLBACK = 50
LOGIN_ID_ENV_VARS = ("EFESTA_USER_ID", "EFESTA_LOGIN_ID", "EFESTA_ID")
LOGIN_PASS_ENV_VARS = (
    "EFESTA_USER_PASS",
    "EFESTA_USER_PASSWORD",
    "EFESTA_PASS",
    "EFESTA_PASSWORD",
)
START_BUTTON_SELECTORS = [
    "#ctl00_masterMain_dkgSubjectTop_hplStart",
    "a#ctl00_masterMain_dkgSubjectTop_hplStart",
    "a[href*=\"__doPostBack('ctl00$masterMain$dkgSubjectTop$hplStart'\"]",
    "text=開始する",
    "text=/開[\\s\\u3000]*始[\\s\\u3000]*す[\\s\\u3000]*る/",
    "button:has-text('開始する')",
    "input[type='button'][value='開始する']",
    "input[type='submit'][value='開始する']",
]

TRACKING_BLOCK_PATTERNS = (
    "google-analytics.com",
    "googletagmanager.com",
    "bat.bing.com",
    "yahoo.co.jp/analytics",
    "adservice.google",
    "/gtag/js",
)
FAST_LOGIN_BLOCK_RESOURCE_TYPES = {"image", "media", "font"}

load_dotenv()

def _as_bool_env(var_name: str, default: str = "1") -> bool:
    raw = os.getenv(var_name, default)
    if raw is None:
        return False
    return raw.strip().lower() not in {"0", "false", "off", "no"}


def _as_int_env(var_name: str, default: int) -> int:
    raw = os.getenv(var_name)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


DEFAULT_TIMEOUT_MS = _as_int_env("EFESTA_TIMEOUT_MS", 5000)
DEFAULT_NAV_TIMEOUT_MS = _as_int_env("EFESTA_NAV_TIMEOUT_MS", 10000)
LOGIN_NAV_TIMEOUT_MS = _as_int_env("EFESTA_LOGIN_TIMEOUT_MS", 30000)
RESTART_GRACE_MS = _as_int_env("EFESTA_RESTART_GRACE_MS", 800)
AUTO_CLICK_SWEEP_MS = _as_int_env("EFESTA_AUTO_CLICK_SWEEP_MS", 4000)

ALLOW_MANUAL_RESTART = _as_bool_env("EFESTA_ALLOW_MANUAL", "1")
EFESTA_NO_FOCUS = _as_bool_env("EFESTA_NO_FOCUS", "1")
SKIP_ON_FAIL = _as_bool_env("EFESTA_SKIP_ON_FAIL", "1")
HEADLESS_MODE = _as_bool_env("EFESTA_HEADLESS", "0")
STRICT_VIEWER = _as_bool_env("EFESTA_STRICT_VIEWER", "0")
COOP_MODE = _as_bool_env("EFESTA_COOP", "1")
SKIP_NEXT_COOP = False
BLOCK_TRACKERS = _as_bool_env("EFESTA_BLOCK_TRACKERS", "0")
FAST_LOGIN_MODE = _as_bool_env("EFESTA_FAST_LOGIN", "0")
FAST_LOGIN_ACTIVE = False


def safe_focus(page: Page | None):
    if not page:
        return
    try:
        if page.is_closed():
            return
    except Exception:
        return
    if EFESTA_NO_FOCUS:
        return
    try:
        page.bring_to_front()
    except Exception:
        pass


def _consume_skip_next_coop() -> bool:
    global SKIP_NEXT_COOP
    if SKIP_NEXT_COOP:
        SKIP_NEXT_COOP = False
        return True
    return False


def _await_user_ready_before_questions(viewer: Page | None, label: str):
    if not COOP_MODE or not viewer:
        return
    logt("coop-wait: enter")
    try:
        viewer.evaluate(
            """
            (lbl) => {
                try {
                    let bar = document.getElementById('ef-coop-bar');
                    if (!bar) {
                        bar = document.createElement('div');
                        bar.id = 'ef-coop-bar';
                        bar.style.cssText = 'position:fixed;z-index:2147483647;top:0;left:0;right:0;height:32px;background:#222;color:#fff;font:13px/32px system-ui;padding:0 10px;opacity:.9';
                        document.body.appendChild(bar);
                    }
                    bar.textContent = '準備完了で Enter ▶ ' + lbl + ' を開始します（必要な手操作を先に実施してください）';
                } catch (err) {}
            }
            """,
            label,
        )
    except Exception:
        pass


def _click_start_button(scope: Page | Frame, timeout_ms: int = 4000) -> bool:
    for selector in START_BUTTON_SELECTORS:
        try:
            locator = scope.locator(selector)
        except Exception:
            continue
        try:
            if locator.count() == 0:
                continue
        except Exception:
            continue
        target = locator.first
        try:
            target.wait_for(state="visible", timeout=timeout_ms)
        except Exception:
            pass
        try:
            target.scroll_into_view_if_needed(timeout=timeout_ms)
        except Exception:
            pass
        try:
            target.click(timeout=timeout_ms)
            return True
        except Exception:
            try:
                target.dispatch_event("click")
                return True
            except Exception:
                try:
                    target.evaluate(
                        "el => { el.click(); el.dispatchEvent(new Event('click', { bubbles: true })); }"
                    )
                    return True
                except Exception:
                    continue
    return False


def _auto_click_start_from_page(page: Page | None, timeout_ms: int = 4000) -> bool:
    if not page:
        return False
    logt("auto_click_start: begin")
    safe_focus(page)
    scopes: list[Page | Frame] = [page]
    try:
        scopes.extend(page.frames or [])
    except Exception:
        pass
    for scope in scopes:
        try:
            if _click_start_button(scope, timeout_ms=timeout_ms):
                print("[restart] Automatically clicked '開始する'.")
                logt("auto_click_start: clicked")
                return True
        except Exception:
            continue
    logt("auto_click_start: give up")
    return False

    print(f"[coop] 必要な手操作を行ってください。完了したら Enter で {label} を開始します。")
    try:
        input()
    except Exception:
        pass

    try:
        viewer.evaluate(
            "() => { const el = document.getElementById('ef-coop-bar'); if (el) { el.remove(); } }"
        )
    except Exception:
        pass
    logt("coop-wait: exit")

SUMMARY_TABLE_SCRIPT = """
() => {
    const pickText = (cell) => {
        if (!cell) { return ""; }
        const titled = cell.querySelector('[title]');
        if (titled && titled.getAttribute('title')) {
            return titled.getAttribute('title').trim();
        }
        return (cell.innerText || cell.textContent || "").trim();
    };
    const tables = Array.from(document.querySelectorAll('table'));
    for (const tbl of tables) {
        const headers = Array.from(tbl.querySelectorAll('th'))
            .map((th) => (th.innerText || "").trim())
            .filter(Boolean);
        if (!headers.length) { continue; }
        const joined = headers.join("|");
        if (!joined.includes("問題") || !joined.includes("解答")) { continue; }
        const rows = [];
        for (const tr of tbl.querySelectorAll('tr')) {
            const cells = Array.from(tr.querySelectorAll('td'));
            if (!cells.length) { continue; }
            rows.push({
                label: pickText(cells[0]),
                answered: pickText(cells[1]),
                correct: pickText(cells[2]),
                judgement: pickText(cells[3]),
            });
        }
        if (rows.length) {
            return rows;
        }
    }
    return null;
}
"""

ANSWER_REVEAL_SCRIPT = """
() => {
    const pickText = (node) => {
        if (!node) { return ""; }
        return (node.innerText || node.textContent || "").trim();
    };
    const root = document.querySelector('.style2') || document.body;
    if (!root) { return null; }
    const markers = ["解説", "正解", "正答", "模範解答", "解答例", "答え"];
    const headings = Array.from(root.querySelectorAll('h1, h2, h3, h4, .style3, .style4, .style5'));
    for (const heading of headings) {
        const label = pickText(heading);
        if (!label) { continue; }
        if (!markers.some((m) => label.includes(m))) { continue; }
        let cursor = heading;
        while (cursor && cursor !== root) {
            cursor = cursor.nextElementSibling;
            if (!cursor) { break; }
            const candidate = pickText(cursor);
            if (candidate) {
                return { text: candidate, marker: label };
            }
        }
    }
    const lines = (root.innerText || "")
        .split(/\\r?\\n/)
        .map((line) => line.trim())
        .filter(Boolean);
    for (let i = 0; i < lines.length; i += 1) {
        if (!markers.some((m) => lines[i].includes(m))) { continue; }
        for (let j = i + 1; j < lines.length; j += 1) {
            const candidate = lines[j];
            if (!candidate) { continue; }
            if (markers.some((m) => candidate.includes(m))) { continue; }
            if (candidate.length <= 1) { continue; }
            return { text: candidate, marker: lines[i] };
        }
    }
    return null;
}
"""


def resolve_env_value(candidates: tuple[str, ...]) -> str | None:
    for key in candidates:
        value = os.getenv(key)
        if value is None:
            continue
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def normalize_login_user_id(raw: str) -> str:
    text = (raw or "").strip()
    if text.upper().startswith("ITS-"):
        return text[4:]
    return text


def highlight_login_inputs(page: Page):
    try:
        highlighted = page.evaluate(
            """
            (color) => {
                const decorate = (selector, placeholder) => {
                    const el = document.querySelector(selector);
                    if (!el) { return false; }
                    el.style.outline = `3px solid ${color}`;
                    el.style.boxShadow = `0 0 10px ${color}`;
                    el.style.borderRadius = "4px";
                    el.style.backgroundColor = "#fffdef";
                    el.style.position = el.style.position || "relative";
                    el.setAttribute("data-efesta-highlight", "1");
                    if (!el.getAttribute("placeholder")) {
                        el.setAttribute("placeholder", placeholder);
                    }
                    return true;
                };
                const idMarked = decorate("input[name='user_id']", "ユーザーIDを入力");
                const pwMarked = decorate("input[name='user_pass']", "パスワードを入力");
                return idMarked || pwMarked;
            }
            """,
            "rgba(255, 183, 3, 0.9)",
        )
        if highlighted:
            print("[login] ユーザーIDとパスワード欄を強調表示しました。")
    except Exception as exc:
        print(f"[login] 入力欄の強調表示に失敗しました: {exc}")


def auto_login_to_portal(page: Page, timeout_ms: int = 12000) -> bool:
    user_id = resolve_env_value(LOGIN_ID_ENV_VARS)
    user_pass = resolve_env_value(LOGIN_PASS_ENV_VARS)
    if not user_id or not user_pass:
        raise RuntimeError(
            "Set EFESTA_USER_ID (or EFESTA_LOGIN_ID / EFESTA_ID) and "
            "EFESTA_USER_PASS (or EFESTA_PASS / EFESTA_PASSWORD) before running this script."
        )

    user_id_input = page.locator("input[name='user_id']")
    user_pass_input = page.locator("input[name='user_pass']")
    try:
        user_id_input.wait_for(state="visible", timeout=timeout_ms)
        user_pass_input.wait_for(state="visible", timeout=timeout_ms)
    except PWTimeoutError:
        print("[login] Login form was not detected. Continuing without automated submission.")
        return False

    highlight_login_inputs(page)

    try:
        user_id_input.scroll_into_view_if_needed(timeout=timeout_ms)
    except Exception:
        pass
    try:
        user_pass_input.scroll_into_view_if_needed(timeout=timeout_ms)
    except Exception:
        pass

    normalized_id = normalize_login_user_id(user_id)
    user_id_input.fill(normalized_id)
    user_pass_input.fill(user_pass)

    submit_clicked = False
    submit_btn = page.locator("input[type='submit']")
    try:
        if submit_btn.count():
            submit_btn.first.click(timeout=timeout_ms)
            submit_clicked = True
    except Exception as exc:
        print(f"[login] Failed to click the login button automatically: {exc}")

    if not submit_clicked:
        try:
            user_pass_input.press("Enter")
            submit_clicked = True
        except Exception:
            pass

    if submit_clicked:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=max(timeout_ms, 10000))
        except PWTimeoutError:
            pass
        print("[login] Submitted the START_URL login form automatically.")
        return True

    print("[login] Unable to submit the login form automatically.")
    return False


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_revealed_answer(raw: str) -> str:
    if not raw:
        return ""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return ""
    candidate = lines[0]
    for token in ("（", "(", "｟", "【", "〔", "["):
        if token in candidate:
            candidate = candidate.split(token, 1)[0].strip()
    candidate = normalize_text(candidate)
    return candidate


def wait_for_frame(page, name: str, timeout_s: float = 20.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        frame = page.frame(name=name)
        if frame:
            return frame
        time.sleep(0.2)
    raise RuntimeError(f"iframe '{name}' was not found.")


def ensure_viewer_frames(page):
    for fname in VIEWER_FRAME_NAMES:
        wait_for_frame(page, fname, timeout_s=20.0)


def ensure_viewer_frames_with_retry(page, retries: int = 3):
    delays = [0.4, 0.6, 0.8]
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            ensure_viewer_frames(page)
            return True
        except Exception as exc:
            last_exc = exc
            wait = delays[attempt] if attempt < len(delays) else delays[-1]
            print(
                f"[viewer] frames not ready (try {attempt + 1}/{retries}): {exc} -> sleep {wait:.1f}s"
            )
            try:
                safe_focus(page)
            except Exception:
                pass
            time.sleep(wait)
    raise RuntimeError(f"viewer frames not ready: {last_exc}")


def detect_total_questions(page, fallback: int = MAX_Q_FALLBACK) -> int:
    try:
        q_frame = wait_for_frame(page, "qlist", timeout_s=6.0)
        count = q_frame.evaluate("document.querySelectorAll(\"table[id^='q']\").length")
        if isinstance(count, int) and count > 0:
            return count
    except Exception:
        pass
    return fallback


def extract_question_text_from_frame(frame) -> str:
    selectors = [
        "css=div.style2 p.question",
        "css=.question",
        "css=div.style2",
        "css=body",
    ]
    for sel in selectors:
        try:
            target = frame.locator(sel)
            target.wait_for(state="visible", timeout=3000)
            raw = (target.inner_text() or "").strip()
            if raw:
                cleaned = normalize_text(raw)
                if cleaned:
                    return cleaned
        except Exception:
            continue
    try:
        alt_texts = frame.evaluate(
            """
            () => {
                const imgs = Array.from(document.images);
                const alts = imgs
                    .map((img) => img.alt || "")
                    .filter(Boolean);
                if (alts.length) {
                    return alts.join(" ");
                }
                const names = imgs
                    .map((img) => {
                        try {
                            const url = new URL(img.src);
                            const fname = (url.pathname.split("/").pop() || "").replace(/\\.[a-z0-9]+$/i, "");
                            return fname;
                        } catch (_) {
                            return "";
                        }
                    })
                    .filter(Boolean);
                return names.join(" ");
            }
            """
        )
        if isinstance(alt_texts, str) and alt_texts.strip():
            return normalize_text(alt_texts)
    except Exception:
        pass
    return "【画像主体の設問】"


def wait_for_next_button(ctrl_frame, timeout_ms: int = 8000):
    ctrl_frame.wait_for_function(
        """
        () => {
            const btn = document.querySelector('#btn_next');
            if (!btn) { return false; }
            const cls = (btn.className || '').toLowerCase();
            return !cls.includes('disable');
        }
        """,
        timeout=timeout_ms,
    )


def _question_fingerprint(page) -> str:
    try:
        contents = wait_for_frame(page, "contents", timeout_s=3.0)
    except RuntimeError:
        return "no-contents"
    try:
        fp = contents.evaluate(
            """
            () => {
                const root = document.querySelector('div.style2') || document.body;
                const txt = (root.innerText || '').replace(/\\s+/g, ' ').slice(0, 200);
                const imgs = Array.from(root.querySelectorAll('img')).map((img) => {
                    try {
                        return new URL(img.src, location.href).pathname.split('/').pop();
                    } catch (_err) {
                        return img.src || '';
                    }
                }).join('|');
                let idx = '';
                try {
                    const qdoc = parent.frames['qlist']?.document;
                    if (qdoc) {
                        const cur = qdoc.querySelector(".selected, .active, td[class*='sel'], td[class*='act'], td[class*='current']");
                        idx = (cur?.innerText || '').match(/\\d+/)?.[0] || '';
                    }
                } catch (_inner) {}
                return JSON.stringify({ txt, imgs, idx });
            }
            """
        )
        if isinstance(fp, str) and fp:
            return fp
    except Exception:
        pass
    return "fp-missing"


def submit_answer(page, answer: str, capture_hook: Callable[[], str | None] | None = None) -> str | None:
    right_frame = wait_for_frame(page, "right", timeout_s=8.0)
    box = right_frame.locator("#div_3_answer")
    box.wait_for(state="visible", timeout=4000)
    try:
        box.scroll_into_view_if_needed(timeout=1500)
    except Exception:
        pass
    try:
        box.click(force=True, timeout=800)
    except Exception:
        pass

    ok = False
    try:
        ok = right_frame.evaluate(
            """
            (value) => {
                const root = document.querySelector("#div_3_answer");
                if (!root) { return false; }
                const editable =
                    root.querySelector("textarea, input[type=text], input[type=search]") ||
                    Array.from(root.querySelectorAll("*")).find((el) => el.isContentEditable);
                if (!editable) { return false; }
                const fire = (el, type) => el.dispatchEvent(new Event(type, { bubbles: true }));
                if (editable.isContentEditable) {
                    editable.focus();
                    editable.textContent = "";
                    fire(editable, "input");
                    editable.textContent = value;
                    fire(editable, "input");
                } else {
                    editable.focus();
                    editable.value = "";
                    fire(editable, "input");
                    editable.value = value;
                    fire(editable, "input");
                    fire(editable, "change");
                }
                return true;
            }
            """,
            answer,
        )
    except Exception:
        ok = False

    if not ok:
        try:
            box.click(force=True, timeout=1500)
            target = right_frame.locator(
                "#div_3_answer textarea, #div_3_answer input[type=text], #div_3_answer input[type=search]"
            )
            if target.count():
                target.first.fill("")
                target.first.type(answer, delay=5)
            else:
                box.fill("")
                box.type(answer, delay=5)
        except Exception:
            pass

    submit_selectors = [
        "img[name='btnKaito2']",
        "xpath=//img[@name='btnKaito2']/ancestor-or-self::*[self::a or self::button or self::div][1]",
        "text=解答する",
        "input[type='button'][value='解答する']",
        "input[type='submit'][value='解答する']",
        "role=button[name='解答する']",
    ]
    clicked = False
    for sel in submit_selectors:
        try:
            btn = right_frame.locator(sel)
            if not btn.count():
                continue
            btn.first.scroll_into_view_if_needed(timeout=1000)
            btn.first.click(force=True, no_wait_after=True, timeout=800)
            clicked = True
            break
        except Exception:
            continue
    if not clicked:
        try:
            right_frame.evaluate(
                """
                () => {
                    const cand = [
                        ...document.querySelectorAll("img[name='btnKaito2']"),
                        ...document.querySelectorAll("button, input[type=button], input[type=submit], a")
                    ];
                    for (const el of cand) {
                        const t = (el.innerText || el.value || "").trim();
                        if (/解答/.test(t) || el.getAttribute("name") === "btnKaito2") {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }
                """
            )
        except Exception:
            pass

    captured: str | None = None
    if capture_hook:
        try:
            captured = capture_hook()
        except Exception as exc:
            print(f"[reveal] Capture hook failed: {exc}")

    ctrl_frame = wait_for_frame(page, "ctrl", timeout_s=8.0)
    try:
        wait_for_next_button(ctrl_frame, timeout_ms=6000)
    except PWTimeoutError:
        wait_for_next_button(ctrl_frame, timeout_ms=6000)
    _click_next_robust(page, ctrl_frame=ctrl_frame)
    time.sleep(0.2)
    return captured


def _click_next_robust(page, ctrl_frame=None) -> None:
    try:
        frame = ctrl_frame or wait_for_frame(page, "ctrl", timeout_s=5.0)
    except RuntimeError:
        return
    try:
        frame.locator("#btn_next").click(force=True, timeout=800, no_wait_after=True)
        return
    except Exception:
        pass
    try:
        frame.evaluate(
            "() => { const b = document.querySelector('#btn_next'); if (b) { b.click(); return true; } return false; }"
        )
    except Exception:
        pass


def _force_move_to_next_index(page) -> bool:
    try:
        ql = wait_for_frame(page, "qlist", timeout_s=3.0)
    except RuntimeError:
        return False
    try:
        return bool(
            ql.evaluate(
                """
                () => {
                    const all = Array.from(
                        document.querySelectorAll("td, .qno, .number, span")
                    ).filter((node) => /^\\d+$/.test((node.innerText || '').trim()));
                    if (!all.length) {
                        return false;
                    }
                    let curIdx = -1;
                    for (let i = 0; i < all.length; i++) {
                        const el = all[i];
                        const cs = getComputedStyle(el);
                        if ((el.className || '').match(/sel|act|selected|active/i)
                            || (cs.borderColor || '').match(/rgb\\(255,\\s*0,\\s*0\\)|#f00|red/i)) {
                            curIdx = i;
                            break;
                        }
                    }
                    const nextEl = (curIdx >= 0 && curIdx + 1 < all.length) ? all[curIdx + 1] : null;
                    if (nextEl) {
                        nextEl.scrollIntoView({ block: 'center' });
                        nextEl.click();
                        return true;
                    }
                    return false;
                }
                """
            )
        )
    except Exception:
        return False


def wait_for_question_change(page, previous_fp: str, timeout_ms: int = 10000) -> bool:
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        try:
            fp = _question_fingerprint(page)
            if fp and fp != previous_fp:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False

def pick_viewer_page(ctx, timeout_s: float = 20.0, exclude_pages: set | None = None):
    deadline = time.time() + timeout_s
    exclude_ids = {id(p) for p in (exclude_pages or [])}
    fallback = ctx.pages[-1] if ctx.pages else None
    while time.time() < deadline:
        for p in ctx.pages:
            if exclude_ids and id(p) in exclude_ids:
                continue
            url = (p.url or "").lower()
            if "/viewer/" in url:
                logt("pick_viewer_page: detected viewer")
                return p
        if ctx.pages:
            fallback = ctx.pages[-1]
        time.sleep(0.5)
    if fallback and exclude_ids and id(fallback) in exclude_ids:
        return None
    return fallback


def poll_viewer_fast(
    ctx,
    duration_ms: int = 3000,
    interval_ms: int = 250,
    exclude_pages: set | None = None,
):
    deadline = _now_ms() + duration_ms
    while _now_ms() < deadline:
        page = pick_viewer_page(ctx, timeout_s=0.2, exclude_pages=exclude_pages)
        if page and _is_viewer_like(page):
            logt("poll_viewer_fast: hit")
            return page
        time.sleep(max(interval_ms, 50) / 1000.0)
    return None


def close_old_viewers(ctx: BrowserContext, keep: Page | None):
    if not ctx:
        return
    for page in list(ctx.pages):
        try:
            if page.is_closed():
                continue
            if keep is not None and page is keep:
                continue
            url = (page.url or "").lower()
            if "/viewer/" in url:
                page.close(ignore_beforeunload=True)
        except Exception:
            pass


def _is_viewer_like(page: Page | None) -> bool:
    if not page:
        return False
    try:
        url = (page.url or "").lower()
        if "/viewer/" in url:
            return True
    except Exception:
        pass
    try:
        for frame in page.frames:
            name = (frame.name or "").lower()
            if name in {"contents", "right", "ctrl", "qlist"}:
                return True
            try:
                if frame.locator("#div_3_answer").count() > 0:
                    return True
                if frame.locator("#btn_next").count() > 0:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _wait_manual_start(ctx, summary_page: Page | None, wait_for_viewer: float = 40.0) -> Page | None:
    if not ALLOW_MANUAL_RESTART or not ctx or not summary_page:
        return None
    safe_focus(summary_page)
    try:
        list_frame = summary_page.frame(name="list")
    except Exception:
        list_frame = None
    if list_frame:
        try:
            list_frame.evaluate(
                """
                () => {
                    try {
                        const link = document.getElementById('ctl00_masterMain_dkgSubjectTop_hplStart');
                        if (link) {
                            link.style.outline = '3px solid orange';
                            link.style.boxShadow = '0 0 10px orange';
                            link.scrollIntoView({ block: 'center', behavior: 'instant' });
                        }
                    } catch (err) {}
                }
                """
            )
        except Exception:
            pass

    stop = threading.Event()

    def watchdog():
        try:
            while not stop.is_set():
                try:
                    _auto_click_start_from_page(summary_page)
                except Exception:
                    pass
                stop.wait(1.0)
        except Exception:
            pass

    threading.Thread(target=watchdog, daemon=True).start()

    print("[manual] Click '開始する' manually, then press Enter when the viewer appears (Q to cancel).")
    logt("manual-wait: begin")
    user_input = ""
    try:
        user_input = input().strip().lower()
    except Exception:
        user_input = ""
    finally:
        stop.set()

    if user_input == "q":
        logt("manual-wait: abort by user")
        return None

    logt("manual-wait: enter pressed")
    viewer = poll_viewer_fast(ctx, duration_ms=int(wait_for_viewer * 1000), interval_ms=250)
    if not viewer:
        viewer = pick_viewer_page(ctx, timeout_s=2.0)
    if viewer:
        safe_focus(viewer)
        global SKIP_NEXT_COOP
        SKIP_NEXT_COOP = True
    return viewer


def _prompt_user_for_viewer(ctx, label: str, timeout_s: float = 60.0) -> Page | None:
    if not ctx:
        return None
    while True:
        print(f"[manual] {label} viewer was not detected. Press Enter to retry detection, or Q to abort.")
        try:
            resp = input().strip().lower()
        except Exception:
            resp = ""
        if resp == "q":
            return None
        candidate = pick_viewer_page(ctx, timeout_s=3.0)
        if not candidate:
            print("[manual] Viewer still not detected. Waiting briefly before the next retry.")
            continue
        if STRICT_VIEWER:
            try:
                ensure_viewer_frames_with_retry(candidate, retries=1)
                return candidate
            except Exception as exc:
                print(f"[manual] viewer frames not ready yet ({exc}). 再度表示して Enter を押してください。")
                continue
        if not _is_viewer_like(candidate):
            deadline = time.time() + 2.0
            while time.time() < deadline and not _is_viewer_like(candidate):
                time.sleep(0.2)
        if _is_viewer_like(candidate):
            global SKIP_NEXT_COOP
            SKIP_NEXT_COOP = True
            return candidate
        print("[manual] viewer らしい画面が確認できませんでしたが、今回の画面を採用します。")
        SKIP_NEXT_COOP = True
        return candidate


def auto_accept_dialogs(page):
    if getattr(page, "_auto_dialog_handler_set", False):
        return

    def _handler(dialog):
        print(f"[dialog] {dialog.message}")
        try:
            dialog.accept()
        except Exception as exc:
            print(f"[dialog] accept failed: {exc}")

    page.on("dialog", _handler)
    setattr(page, "_auto_dialog_handler_set", True)


def attach_dialog_handler_to_all(ctx: BrowserContext | None):
    if not ctx:
        return

    def _handler(dialog):
        print(f"[dialog] {dialog.message}")
        try:
            dialog.accept()
        except Exception as exc:
            print(f"[dialog] accept failed: {exc}")

    def _attach(page):
        if not page or getattr(page, "_auto_dialog_handler_set", False):
            return
        try:
            page.on("dialog", _handler)
            setattr(page, "_auto_dialog_handler_set", True)
        except Exception:
            pass

    for page in getattr(ctx, "pages", []):
        _attach(page)

    if getattr(ctx, "_auto_dialog_handler_set", False):
        return

    def _on_new_page(page):
        _attach(page)

    try:
        ctx.on("page", _on_new_page)
        setattr(ctx, "_auto_dialog_handler_set", True)
    except Exception:
        pass


def find_summary_page(ctx: BrowserContext | None, preferred_pages: list[Page] | None = None) -> Page | None:
    if not ctx:
        return None

    seen = set()

    def _iter_candidates():
        if preferred_pages:
            for page in preferred_pages:
                if not page or page.is_closed():
                    continue
                ident = id(page)
                if ident in seen:
                    continue
                seen.add(ident)
                yield page
        for page in getattr(ctx, "pages", []):
            if not page or page.is_closed():
                continue
            ident = id(page)
            if ident in seen:
                continue
            seen.add(ident)
            yield page

    markers = ("テスト結果", "受講結果", "結果一覧", "受講履歴")
    url_keys = ("summary", "result", "complete")
    for page in _iter_candidates():
        try:
            url = (page.url or "").lower()
        except Exception:
            url = ""
        try:
            if any(key in url for key in url_keys):
                return page
            for marker in markers:
                try:
                    locator = page.locator(f"text={marker}")
                    if locator.count() > 0:
                        return page
                except Exception:
                    continue
            link_count = page.locator("a:has-text('戻る')").count()
            start_btn = page.locator("text=開始する").count()
            if link_count and start_btn:
                return page
        except Exception:
            continue
    return None


def trigger_results_popup(page, wait_s: float = 6.0) -> bool:
    try:
        ctrl_frame = wait_for_frame(page, "ctrl", timeout_s=8.0)
    except RuntimeError:
        return False

    btn = ctrl_frame.locator("#btn_summary")
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            btn.wait_for(state="visible", timeout=1000)
            btn.click(force=True)
            try:
                btn.evaluate(
                    """(node) => {
                        if (!node) { return; }
                        node.disabled = true;
                        setTimeout(() => { node.disabled = false; }, 1500);
                    }"""
                )
            except Exception:
                pass
            time.sleep(1.2)
            return True
        except PWTimeoutError:
            continue
        except Exception as exc:
            message = str(exc).lower()
            if "detached" in message or "closed" in message:
                time.sleep(0.5)
                try:
                    ctrl_frame = wait_for_frame(page, "ctrl", timeout_s=2.0)
                    btn = ctrl_frame.locator("#btn_summary")
                except Exception:
                    break
                continue
            print(f"[results] Summary button click failed: {exc}")
            break
    return False


AnswerProvider = Callable[[int, str], str]


def random_wrong_answer(idx: int) -> str:
    suffix = random.choice(string.ascii_lowercase + string.digits)
    return f"x{idx}{suffix}"


def capture_revealed_answer(page, timeout_s: float = 6.0) -> str:
    deadline = time.time() + timeout_s
    try:
        contents_frame = wait_for_frame(page, "contents", timeout_s=timeout_s)
    except RuntimeError:
        return ""

    last_value = ""
    while time.time() < deadline:
        try:
            result = contents_frame.evaluate(ANSWER_REVEAL_SCRIPT)
        except Exception:
            result = None
        if isinstance(result, dict):
            candidate = clean_revealed_answer(result.get("text", ""))
            if candidate:
                last_value = candidate
                break
        time.sleep(0.4)
    return last_value


def answer_quiz(page, answer_provider: AnswerProvider, phase_label: str, capture_reveals: bool = False) -> list[dict]:
    auto_accept_dialogs(page)
    try:
        if not _is_viewer_like(page):
            ensure_viewer_frames_with_retry(page, retries=3)
    except RuntimeError as exc:
        print(f"[{phase_label}] Required iframes were missing: {exc}")
        return []

    total_questions = detect_total_questions(page, fallback=MAX_Q_FALLBACK)
    print(f"[{phase_label}] Targeting {total_questions} questions.")
    history: list[dict] = []

    for idx in range(1, total_questions + 1):
        try:
            contents_frame = wait_for_frame(page, "contents", timeout_s=8.0)
            question_text = extract_question_text_from_frame(contents_frame)
            question_fp = _question_fingerprint(page)
        except Exception as exc:
            print(f"[{phase_label} #{idx}] Unable to read question: {exc}")
            break

        answer = answer_provider(idx, question_text)
        if not isinstance(answer, str):
            answer = str(answer or "")
        cleaned_answer = answer.strip() or random_wrong_answer(idx)

        history.append(
            {
                "index": idx,
                "question": question_text,
                "answer": cleaned_answer,
                "phase": phase_label,
            }
        )

        short_q = question_text[:40] + ("..." if len(question_text) > 40 else "")
        print(f"[{phase_label} #{idx}] -> {cleaned_answer} | {short_q}")

        reveal_hook = (lambda: capture_revealed_answer(page)) if capture_reveals else None

        try:
            revealed_answer = submit_answer(page, cleaned_answer, capture_hook=reveal_hook)
        except Exception as exc:
            print(f"[{phase_label} #{idx}] Submit failed: {exc}")
            break

        if capture_reveals and revealed_answer:
            history[-1]["revealed_answer"] = revealed_answer
            print(f"[{phase_label} #{idx}] Revealed answer -> {revealed_answer}")

        if idx == total_questions:
            print(f"[{phase_label}] Completed run of {idx} questions.")
            break

        if not wait_for_question_change(page, question_fp, timeout_ms=8000):
            print(f"[{phase_label} #{idx}] Next question did not load. Forcing navigation...")
            _click_next_robust(page)
            if not wait_for_question_change(page, question_fp, timeout_ms=5000):
                moved = _force_move_to_next_index(page)
                if not moved:
                    _click_next_robust(page)
                if not wait_for_question_change(page, question_fp, timeout_ms=5000):
                    print(f"[{phase_label} #{idx}] Next question still unavailable. Aborting run.")
                    break

    return history


def iter_targets(root):
    seen = set()
    stack = [root]
    while stack:
        target = stack.pop()
        ident = id(target)
        if ident in seen:
            continue
        seen.add(ident)
        yield target
        frames = []
        try:
            if hasattr(target, "frames"):
                frames = target.frames
            elif hasattr(target, "child_frames"):
                frames = target.child_frames
        except Exception:
            frames = []
        stack.extend(frames)


def extract_summary_rows(page) -> list[dict]:
    for target in iter_targets(page):
        try:
            rows = target.evaluate(SUMMARY_TABLE_SCRIPT)
        except Exception:
            rows = None
        if rows:
            return rows
    return []


def parse_question_number(label: str) -> int | None:
    if not label:
        return None
    match = re.search(r"(\\d+)", label)
    if match:
        return int(match.group(1))
    return None


def acknowledge_completion_screen(ctx, timeout_s: float = 12.0) -> Page | None:
    selectors = [
        "button:has-text('OK')",
        "button:has-text('Ok')",
        "button:has-text('ＯＫ')",
        "input[type='button'][value='OK']",
        "input[type='button'][value='Ok']",
        "input[type='button'][value='ＯＫ']",
        "input[type='submit'][value='OK']",
        "input[type='submit'][value='Ok']",
        "input[type='submit'][value='ＯＫ']",
        "input[alt='OK']",
        "input[alt='ＯＫ']",
        "a:has-text('OK')",
        "a:has-text('Ok')",
        "a:has-text('ＯＫ')",
    ]
    deadline = time.time() + timeout_s

    def _page_priority(page: Page) -> int:
        try:
            if page.is_closed():
                return 0
            if page.locator("[role='dialog']").count() > 0:
                return 2
            if page.locator("text=テスト結果").count() > 0:
                return 1
        except Exception:
            return 0
        return 0

    while time.time() < deadline:
        active_pages = [page for page in ctx.pages if not page.is_closed()]
        active_pages.sort(key=_page_priority, reverse=True)
        for page in active_pages:
            scopes: list = []
            scope_selectors = [
                "#facebox .content",
                "#facebox",
                "[role='dialog']",
                ".modal",
                ".modal-dialog",
                "xpath=//*[contains(@class,'facebox') or @role='dialog']",
            ]
            for scope_selector in scope_selectors:
                try:
                    scoped = page.locator(scope_selector)
                except Exception:
                    continue
                if scoped.count():
                    scopes.append(scoped)
            scopes.append(page)
            for scope in scopes:
                for selector in selectors:
                    try:
                        locator = scope.locator(selector)
                        if locator.count() == 0:
                            continue
                        target = locator.first
                        target.wait_for(state="visible", timeout=1500)
                        target.click()
                        print("[results] Clicked the completion OK button.")
                        return page
                    except PWTimeoutError:
                        continue
                    except Exception:
                        continue
        time.sleep(0.4)
    return None


def build_answer_map_from_history(history: list[dict]) -> dict[int, str]:
    answers: dict[int, str] = {}
    for row in history:
        idx = row.get("index")
        revealed = normalize_text(row.get("revealed_answer", ""))
        if not idx or not revealed:
            continue
        answers[idx] = revealed
    return answers


def build_answer_provider(answer_map: dict[int, str]) -> AnswerProvider:
    def _provider(idx: int, _: str) -> str:
        return answer_map.get(idx, random_wrong_answer(idx))

    return _provider


def auto_restart_quiz(
    ctx,
    summary_page: Page | None,
    previous_viewer: Page | None = None,
    wait_for_viewer: float = 25.0,
) -> Page | None:
    label = "2nd-viewer" if previous_viewer else "initial-session"
    exclude_pages: set[Page] = set()
    if previous_viewer:
        exclude_pages.add(previous_viewer)
    if summary_page:
        exclude_pages.add(summary_page)

    if RESTART_GRACE_MS > 0:
        time.sleep(RESTART_GRACE_MS / 1000.0)

    sweep_ms = max(0, AUTO_CLICK_SWEEP_MS)
    if summary_page and sweep_ms:
        logt("auto_click_sweep: start")
        deadline = time.time() + (sweep_ms / 1000.0)
        while time.time() < deadline:
            if _auto_click_start_from_page(summary_page):
                viewer = poll_viewer_fast(
                    ctx,
                    duration_ms=2500,
                    interval_ms=200,
                    exclude_pages=exclude_pages,
                )
                if viewer:
                    return viewer
            time.sleep(0.3)
        logt("auto_click_sweep: end")

    fast = poll_viewer_fast(
        ctx,
        duration_ms=3000,
        interval_ms=250,
        exclude_pages=exclude_pages,
    )
    if fast:
        return fast

    if summary_page:
        try:
            name_hint = (summary_page.evaluate("() => window.name || ''") or "").lower()
            if "viewer" in name_hint:
                hit = pick_viewer_page(ctx, timeout_s=1.0, exclude_pages=exclude_pages)
                if hit:
                    logt("summary window.name indicates viewer; accept")
                    return hit
        except Exception:
            pass

    print("Viewer not detected within 8s — press Enter to retry detection, or Q to abort.")
    try:
        retry_input = input().strip().lower()
    except Exception:
        retry_input = ""
    if retry_input == "q":
        return None
    retry_hit = poll_viewer_fast(
        ctx,
        duration_ms=4000,
        interval_ms=250,
        exclude_pages=exclude_pages,
    )
    if retry_hit:
        return retry_hit

    viewer = _wait_manual_start(ctx, summary_page, wait_for_viewer) if summary_page else None
    if viewer:
        return viewer

    print("[restart] Viewer could not be detected automatically. Please bring the quiz viewer to the front and press Enter.")
    return _prompt_user_for_viewer(ctx, label, timeout_s=wait_for_viewer)


def run_two_pass_cycle(ctx, initial_viewer: Page) -> tuple[Page | None, Page | None]:
    viewer = initial_viewer
    wrong_provider = lambda idx, _: random_wrong_answer(idx)
    attach_dialog_handler_to_all(ctx)
    wrong_history = answer_quiz(
        viewer,
        wrong_provider,
        phase_label="wrong-run",
        capture_reveals=True,
    )
    if not wrong_history:
        print("Intentional fail run aborted. Nothing else to do.")
        return None, None

    def _fail_and_cleanup(handle: Page | None):
        close_old_viewers(ctx, keep=handle)
        return handle, None

    answers = build_answer_map_from_history(wrong_history)
    expected_questions = max((row.get("index", 0) for row in wrong_history), default=0)
    if answers:
        print(f"[reveal] Captured {len(answers)} answers from the miss screens.")

    trigger_results_popup(viewer)
    attach_dialog_handler_to_all(ctx)
    completion_page = acknowledge_completion_screen(ctx)
    attach_dialog_handler_to_all(ctx)
    summary_page_fail = completion_page or viewer
    if expected_questions and len(answers) < expected_questions:
        missing = expected_questions - len(answers)
        print(f"[reveal] Still missing {missing} answers after reading the summary.")

    if not answers:
        print("Could not read the answer list. Open e-FESTA-ans manually and retry.")
        return _fail_and_cleanup(summary_page_fail)

    viewer = auto_restart_quiz(ctx, summary_page_fail, previous_viewer=viewer, wait_for_viewer=25.0)
    if not viewer:
        print("[restart] Could not detect the viewer for the second pass.")
        return _fail_and_cleanup(summary_page_fail)
    correct_provider = build_answer_provider(answers)
    if not _consume_skip_next_coop():
        _await_user_ready_before_questions(viewer, "2回目（満点）")
    attach_dialog_handler_to_all(ctx)
    perfect_history = answer_quiz(viewer, correct_provider, phase_label="perfect-run")
    if not perfect_history:
        print("Perfect run did not finish. Check the viewer state and try again.")
        return _fail_and_cleanup(summary_page_fail)

    trigger_results_popup(viewer)
    attach_dialog_handler_to_all(ctx)
    final_completion_page = acknowledge_completion_screen(ctx)
    attach_dialog_handler_to_all(ctx)
    summary_page_success = final_completion_page or viewer
    summary_page_final = summary_page_success or summary_page_fail
    print("結果ページを表示しました。履歴の保存などを行ってください。")
    close_old_viewers(ctx, keep=summary_page_final)
    return summary_page_final, viewer


def main():
    with sync_playwright() as p:
        launch_args = [
            "--disable-extensions",
            "--disable-features=TranslateUI",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
        ]
        if not HEADLESS_MODE:
            launch_args.append("--start-maximized")
        browser = p.chromium.launch(headless=HEADLESS_MODE, args=launch_args)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        ctx.set_default_timeout(DEFAULT_TIMEOUT_MS)
        ctx.set_default_navigation_timeout(DEFAULT_NAV_TIMEOUT_MS)

        if BLOCK_TRACKERS or FAST_LOGIN_MODE:
            def _traffic_shaper(route):
                url = route.request.url or ""
                if BLOCK_TRACKERS and any(k in url for k in TRACKING_BLOCK_PATTERNS):
                    return route.abort()
                if FAST_LOGIN_MODE and FAST_LOGIN_ACTIVE:
                    rtype = (route.request.resource_type or "").lower()
                    if rtype in FAST_LOGIN_BLOCK_RESOURCE_TYPES:
                        return route.abort()
                return route.continue_()

            ctx.route("**/*", _traffic_shaper)
        attach_dialog_handler_to_all(ctx)
        landing = ctx.new_page()
        global FAST_LOGIN_ACTIVE
        FAST_LOGIN_ACTIVE = FAST_LOGIN_MODE
        if FAST_LOGIN_ACTIVE:
            print("[login] FAST_LOGIN is enabled; images, media, and fonts will be skipped until the login form is processed.")
        try:
            try:
                landing.goto(
                    START_URL,
                    wait_until="domcontentloaded",
                    timeout=LOGIN_NAV_TIMEOUT_MS,
                )
            except PWTimeoutError:
                print(
                    f"[login] START_URL load exceeded {LOGIN_NAV_TIMEOUT_MS} ms, continuing with partial content."
                )
            try:
                auto_login_to_portal(landing)
            except RuntimeError as exc:
                print(f"[login] {exc}")
                return
        finally:
            FAST_LOGIN_ACTIVE = False

        session_target_raw = os.getenv("EFESTA_SESSIONS")
        session_target = int(session_target_raw) if session_target_raw else None
        session_index = 1
        summary_page: Page | None = None
        last_viewer: Page | None = None

        viewer = _prompt_user_for_viewer(ctx, "1回目（意図的ミス）")
        if not viewer:
            print("Viewer を検出できませんでした。終了します。")
            return

        while session_target is None or session_index <= session_target:
            safe_focus(viewer)
            if session_index > 1:
                _await_user_ready_before_questions(viewer, f"{session_index}回目（開始）")

            summary_page, last_viewer = run_two_pass_cycle(ctx, viewer)
            if not summary_page or not last_viewer:
                print("セッションを完了できませんでした。処理を終了します。")
                return

            session_index += 1
            if session_target and session_index > session_target:
                break

            viewer = auto_restart_quiz(ctx, summary_page, previous_viewer=last_viewer, wait_for_viewer=35.0)
            if not viewer:
                if SKIP_ON_FAIL:
                    print(f"[restart] Session {session_index} was skipped because a viewer did not appear.")
                    continue
                print("Viewer could not be detected. Aborting the automation.")
                return


if __name__ == "__main__":
    main()
