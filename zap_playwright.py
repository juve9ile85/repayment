#!/usr/bin/env python3
# zap_playwright.py
"""
zapPlaywright.js Python 포팅 + unfinishedOrder 페이지 순회 로직.
- 로그인 / key 자동 확보 / /operate API 호출 (자동 갱신 + 재시도)
- 페이지 이동마다 key가 바뀌어도 매 호출마다 fresh key 확보/재사용
"""

import os
import sys
import json
import time
import asyncio
from urllib.parse import urlencode

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError


# =========================================
# ===== 설정 =====
# =========================================
BASE = os.environ.get("ZAP_BASE", "https://cdb.zapbeast.com")
ADMIN_NAME = os.environ.get("ZAP_ADMIN_NAME", "admin")
ADMIN_PASSWORD = os.environ.get("ZAP_ADMIN_PASSWORD", "112233")
HEADLESS = str(os.environ.get("ZAP_HEADLESS", "true")).lower() == "true"

KEY_FRESH_SEC = 55
LANG = "ko-ko"

OUTPUT_JSON = os.environ.get("ZAP_OUTPUT_JSON", "unfinished_orders.json")


# =========================================
# ===== ZAP 클라이언트 =====
# =========================================
class ZapClient:
    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._logged_in = False

    # ---------- 유틸 ----------
    @staticmethod
    def is_key_fresh(latest, max_age_sec=KEY_FRESH_SEC):
        if not latest or not latest.get("time"):
            return False
        now_sec = int(time.time())
        try:
            return (now_sec - int(latest["time"])) < max_age_sec
        except (TypeError, ValueError):
            return False

    async def screenshot(self, tag):
        try:
            path = f"debug-{tag}-{int(time.time() * 1000)}.png"
            await self._page.screenshot(path=path, full_page=False)
            print(f"📸 스크린샷: {path}")
            return path
        except Exception:
            return None

    async def wait_for_key(self, timeout_ms=10000):
        start = time.time()
        while (time.time() - start) * 1000 < timeout_ms:
            latest = await self._page.evaluate("() => window.__LATEST__ || null")
            if latest and latest.get("key"):
                return latest
            await self._page.wait_for_timeout(250)
        return None

    # ---------- 페이지 준비 ----------
    async def get_page(self):
        if self._page and not self._page.is_closed():
            return self._page

        if not self._browser or not self._browser.is_connected():
            if not self._pw:
                self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=HEADLESS,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--window-size=1920,1080",
                ],
            )

        self._context = await self._browser.new_context(
            locale="ko-KR",
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()

        # 봇 감지 우회
        await self._page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages',
                { get: () => ['ko-KR','ko','en-US','en'] });
        """)

        # XHR + fetch 후킹
        await self._page.add_init_script("""
            window.__LATEST__ = null;
            window.__KEY_BY_PATH__ = {};

            const record = (url, headers) => {
                if (!url || !url.includes('/operate/')) return;
                const h = headers || {};
                const key   = h.key   || h.Key   || h['KEY'];
                const time_ = h.time  || h.Time  || h['TIME'];
                const token = h.token || h.Token || h['TOKEN'] || h['authorization'];
                if (key && time_ && token) {
                    const path = url.split('?')[0].replace(/^https?:\\/\\/[^/]+/, '');
                    const entry = { url, path, key, time: time_, token, ts: Date.now() };
                    window.__LATEST__ = entry;
                    window.__KEY_BY_PATH__[path] = entry;
                }
            };

            // XHR
            const OrigOpen  = XMLHttpRequest.prototype.open;
            const OrigSetH  = XMLHttpRequest.prototype.setRequestHeader;
            const OrigSend  = XMLHttpRequest.prototype.send;

            XMLHttpRequest.prototype.open = function (method, url, ...rest) {
                this.__url = url;
                this.__headers = {};
                return OrigOpen.call(this, method, url, ...rest);
            };
            XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
                try { this.__headers[name.toLowerCase()] = value; } catch (e) {}
                return OrigSetH.call(this, name, value);
            };
            XMLHttpRequest.prototype.send = function (...args) {
                const self = this;
                const onDone = () => record(self.__url, self.__headers);
                this.addEventListener('load', onDone);
                this.addEventListener('loadend', onDone);
                return OrigSend.apply(this, args);
            };

            // fetch
            const origFetch = window.fetch;
            window.fetch = async function (input, init) {
                try {
                    const url = (typeof input === 'string') ? input : (input && input.url) || '';
                    const headers = {};
                    const raw = (init && init.headers) || {};
                    if (raw instanceof Headers) {
                        raw.forEach((v, k) => headers[k.toLowerCase()] = v);
                    } else if (Array.isArray(raw)) {
                        raw.forEach(([k, v]) => headers[String(k).toLowerCase()] = v);
                    } else {
                        Object.keys(raw).forEach(k => headers[k.toLowerCase()] = raw[k]);
                    }
                    const res = await origFetch.apply(this, arguments);
                    record(url, headers);
                    return res;
                } catch (e) {
                    return origFetch.apply(this, arguments);
                }
            };
        """)

        return self._page

    # ---------- 로그인 ----------
    async def ensure_login(self):
        if self._logged_in and self._page and not self._page.is_closed():
            return

        page = await self.get_page()

        print("🔐 [zap] 로그인 페이지 접속")
        await page.goto(f"{BASE}/oa/", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)

        async def find_login_frame():
            for f in page.frames:
                try:
                    has_pw = await f.query_selector('input[type="password"]')
                    if has_pw:
                        return f
                except Exception:
                    continue
            return None

        frame = await find_login_frame()

        if not frame:
            print("✅ [zap] 비밀번호 필드 없음 → 이미 로그인 상태")
            self._logged_in = True
            try:
                await self.wait_for_key(5000)
            except Exception:
                pass
            return

        print(f"🔐 [zap] 로그인 폼 발견. frame: {frame.url}")

        name_input = None
        for sel in [
            'input[name="username"]',
            'input[name="id"]',
            'input[name="userId"]',
            'input[type="text"]',
            'input[type="email"]',
        ]:
            name_input = await frame.query_selector(sel)
            if name_input:
                break

        pw_input = await frame.query_selector('input[type="password"]')

        if not name_input or not pw_input:
            await self.screenshot("login-selector-fail")
            raise RuntimeError("아이디/비번 필드 못 찾음")

        await name_input.click()
        await name_input.fill("")
        await name_input.type(ADMIN_NAME, delay=50)

        await pw_input.click()
        await pw_input.fill("")
        await pw_input.type(ADMIN_PASSWORD, delay=50)

        await page.wait_for_timeout(300)

        btn = None
        for sel in [
            'button[type="submit"]',
            'button:has-text("로그인")',
            'button:has-text("Login")',
            'button:has-text("Sign in")',
            'input[type="submit"]',
        ]:
            try:
                btn = await frame.query_selector(sel)
            except Exception:
                btn = None
            if btn:
                break

        if btn:
            await btn.click()
        else:
            print("⚠️ 버튼 없음 → Enter")
            await pw_input.press("Enter")

        print("⏳ 로그인 응답 대기...")
        before_url = page.url
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    page.wait_for_function(
                        "() => !document.querySelector('input[type=\"password\"]')",
                        timeout=20000,
                    ),
                    page.wait_for_url(lambda u: u != before_url, timeout=20000),
                    return_exceptions=True,
                ),
                timeout=21,
            )
        except (asyncio.TimeoutError, PWTimeoutError):
            print("⚠️ 로그인 완료 신호 없음 (계속 진행)")

        await page.wait_for_timeout(3000)

        still_pw = await page.query_selector('input[type="password"]')
        if still_pw:
            await self.screenshot("login-still-pw")
            body = await page.text_content("body") or ""
            print(f"❌ 로그인 실패. body: {body[:300]}")
            raise RuntimeError("로그인 실패 (비밀번호 필드 여전히 존재)")

        print(f"✅ [zap] 로그인 완료. URL: {page.url}")

        latest = await self.wait_for_key(10000)
        if latest:
            print(f"🔑 로그인 후 key: {latest['key'][:12]}...")

        self._logged_in = True

    # ---------- 새 key 확보 ----------
    async def refresh_for_new_key(self, force=False):
        page = await self.get_page()

        # 1) 이미 신선한 key 재사용
        if not force:
            try:
                existing = await page.evaluate("() => window.__LATEST__ || null")
            except Exception:
                existing = None
            if existing and existing.get("key") and self.is_key_fresh(existing):
                age = int(time.time()) - int(existing["time"])
                print(
                    f"♻️ 기존 key 재사용: {existing['key'][:12]}... "
                    f"path: {existing.get('path')} age: {age}s"
                )
                return existing

        print(f"🔄 새 key 확보 시작 (force={force})")

        # [1차] 현재 페이지에서 XHR 트리거
        print("  [1차] 현재 페이지에서 XHR 트리거")
        t = int(time.time() * 1000)
        trigger_urls = [
            f"{BASE}/operate/agency/agentsList?lang={LANG}&_t={t}",
            f"{BASE}/operate/agency/shopManagerList?lang={LANG}&_t={t}",
            f"{BASE}/operate/user/userInfo?lang={LANG}&_t={t}",
            # 페이지 순회 중이면 이 API 자체를 트리거로도 활용 가능
            f"{BASE}/operate/powerbank/unfinishedOrder?page=1&lang={LANG}&_t={t}",
        ]
        for url in trigger_urls:
            try:
                await page.evaluate(
                    """async (u) => {
                        try { await fetch(u, { method: 'GET', credentials: 'include' }); }
                        catch (e) {}
                    }""",
                    url,
                )
            except Exception:
                pass
            await page.wait_for_timeout(600)

            latest = await self.wait_for_key(2000)
            if latest and self.is_key_fresh(latest):
                print(f"🔑 [트리거] key: {latest['key'][:12]}... path: {latest.get('path')}")
                return latest

        # [2차] 해시 이동
        print("  [2차] 해시 이동으로 key 재시도 (페이지 리로드 X)")
        try:
            await page.evaluate("() => { location.hash = '#/agency/shopManager'; }")
            await page.wait_for_timeout(2500)
        except Exception as e:
            print(f"⚠️ 해시 이동 실패: {e}")

        latest = await self.wait_for_key(6000)
        if latest and self.is_key_fresh(latest):
            print(f"🔑 [해시이동] key: {latest['key'][:12]}... path: {latest.get('path')}")
            return latest

        # [3차] 새로고침
        print("  [3차] 새로고침으로 key 재시도")
        try:
            await page.reload(wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2500)
            latest = await self.wait_for_key(8000)
            if latest and self.is_key_fresh(latest):
                print(f"🔑 [새로고침] key: {latest['key'][:12]}...")
                return latest
        except Exception as e:
            print(f"⚠️ 새로고침 실패: {e}")

        # [4차] 재로그인
        print("  [4차] 재로그인 시도")
        try:
            self._logged_in = False
            await self.ensure_login()
            latest = await self.wait_for_key(8000)
            if latest and self.is_key_fresh(latest):
                print(f"🔑 [재로그인] key: {latest['key'][:12]}...")
                return latest
        except Exception as e:
            print(f"⚠️ 재로그인 실패: {e}")

        await self.screenshot("key-fail")
        try:
            dump = await page.evaluate(
                """() => ({
                    latest: window.__LATEST__,
                    paths: Object.keys(window.__KEY_BY_PATH__ || {}),
                    url: location.href,
                })"""
            )
        except Exception:
            dump = {}
        print(f"❌ key 확보 최종 실패. dump={json.dumps(dump, ensure_ascii=False)}")
        return None

    # ---------- 내부: query string ----------
    @staticmethod
    def _build_qs(method, body, query):
        params = {"lang": LANG}
        params.update(query or {})
        if method == "GET" and body:
            for k, v in body.items():
                params[k] = "" if v is None else str(v)
        return urlencode(params)

    # ---------- 내부: fetch evaluate ----------
    async def _do_fetch(self, path, method, body, qs, key, t, token):
        page = await self.get_page()
        return await page.evaluate(
            """async ({ base, key, time, token, path, method, body, qs }) => {
                const url = `${base}${path}?${qs}&_t=${time * 1000 + 123}`;
                const init = {
                    method,
                    headers: {
                        accept: 'application/json, text/plain, */*',
                        'content-type': 'application/json',
                        key: key,
                        time: String(time),
                        token: token,
                        origin: base,
                        referer: `${base}/oa/`,
                    },
                    credentials: 'include',
                };
                if (method !== 'GET' && body != null) {
                    init.body = JSON.stringify(body);
                }
                const res = await fetch(url, init);
                const text = await res.text();
                let json;
                try { json = JSON.parse(text); }
                catch (e) { json = { raw: text }; }
                return { status: res.status, json };
            }""",
            {
                "base": BASE,
                "key": key,
                "time": t,
                "token": token,
                "path": path,
                "method": method,
                "body": body,
                "qs": qs,
            },
        )

    # ---------- API: 자동 갱신 + 재시도 ----------
    async def call_operate_api(self, path, method="GET", body=None, query=None, max_retry=2):
        if not path or not path.startswith("/operate/"):
            raise ValueError(f"path는 /operate/ 로 시작해야 합니다: {path}")
        query = query or {}

        await self.ensure_login()

        qs = self._build_qs(method, body, query)

        last_err = None
        for attempt in range(1, max_retry + 1):
            latest = await self.refresh_for_new_key(force=(attempt > 1))
            if not latest or not latest.get("key"):
                if attempt == max_retry:
                    raise RuntimeError("key 확보 실패")
                print(f"⚠️ key 없음 ({attempt}/{max_retry}) → 재시도")
                continue

            t = int(latest["time"])
            key = latest["key"]
            token = latest["token"]

            print(f"🔑 [attempt {attempt}] {method} {path} key={key[:12]}... time={t}")

            try:
                result = await self._do_fetch(path, method, body, qs, key, t, token)
            except Exception as e:
                last_err = e
                print(f"❌ [attempt {attempt}] fetch 실패: {e}")
                if attempt == max_retry:
                    raise
                continue

            print(
                f"📥 [attempt {attempt}] HTTP {result['status']}: "
                f"{json.dumps(result.get('json'), ensure_ascii=False)[:400]}"
            )

            if result["status"] == 200 and isinstance(result.get("json"), dict) \
                    and result["json"].get("code") == 1:
                return result

            body_str = json.dumps(result.get("json"), ensure_ascii=False)
            is_auth_error = (
                    result["status"] == 403
                    or (isinstance(result.get("json"), dict) and result["json"].get("code") == 403)
                    or any(w in body_str.lower() for w in ("time", "key", "token"))
            )
            if is_auth_error and attempt < max_retry:
                print("⚠️ key/time 만료 의심 → 재시도 (강제 재확보)")
                continue

            return result

        raise RuntimeError(f"API 호출 실패: 재시도 초과 ({last_err})")

    # ---------- API: 확보한 key 재사용 ----------
    async def call_operate_api_with_key(self, path, auth, method="GET", body=None, query=None):
        if not auth or not auth.get("key") or auth.get("time") is None or not auth.get("token"):
            raise ValueError("call_operate_api_with_key: auth{key,time,token} 필수")
        if not path or not path.startswith("/operate/"):
            raise ValueError(f"path는 /operate/ 로 시작해야 합니다: {path}")
        query = query or {}

        qs = self._build_qs(method, body, query)
        return await self._do_fetch(
            path, method, body, qs,
            auth["key"], int(auth["time"]), auth["token"],
        )

    # ---------- 종료 ----------
    async def close(self):
        try:
            if self._page:
                await self._page.close()
        except Exception:
            pass
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None
        self._logged_in = False


# =========================================
# ===== unfinishedOrder 페이지 순회 =====
# =========================================
# =========================================
# ===== unfinishedOrder 페이지 순회 (클라이언트 필터) =====
# =========================================
async def fetch_all_unfinished_orders(client: ZapClient):
    """
    /operate/powerbank/unfinishedOrder 페이지 순회 수집.
    - 서버에는 status="" (전체)로 요청 → 거부(사용량 과다) 회피
    - 응답 중 status == 5 (결제 대기 중)만 파이썬에서 필터링
    - 응답: data 가 바로 배열 → rows 수로 종료 판단
    - 페이지마다 key가 바뀌므로 call_operate_api()의 자동 key 갱신에 의존
    """
    base_body = {
        "member_id": "",
        "uid": "",
        "order_no": "",
        "cabinet_id": "",
        "status": "",          # ★ 서버 필터 비움 (전체 조회)
        "sid": "",
        "sname": "",
        "agency_id": "",
        "agency_name": "",
        "start_time": 1793458800,
        "end_time": 1795964400,
        "export": 0,
        "page_size": 20,
    }

    TARGET_STATUS = "5"  # 결제 대기 중
    page_size = int(base_body["page_size"])

    all_rows = []
    page_no = 1
    max_pages = 500

    while page_no <= max_pages:
        print(f"\n===== unfinishedOrder page={page_no} =====")

        res = await client.call_operate_api(
            "/operate/powerbank/unfinishedOrder",
            method="POST",
            body=base_body,
            query={"page": page_no},
            max_retry=3,
        )

        status = res.get("status")
        js = res.get("json") or {}

        if status != 200 or js.get("code") != 1:
            print(
                f"⚠️ page={page_no} 비정상 응답 status={status} "
                f"json={json.dumps(js, ensure_ascii=False)[:300]}"
            )
            break

        data = js.get("data")

        # data 가 바로 배열
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = []
            for k in ("list", "rows", "items", "records", "data"):
                if isinstance(data.get(k), list):
                    rows = data[k]
                    break
        else:
            rows = []

        # ★ 클라이언트 필터: status == 5 만
        filtered = [r for r in rows if str(r.get("status")) == TARGET_STATUS]

        print(
            f"📦 page={page_no} rows={len(rows)} "
            f"→ 결제대기중 {len(filtered)}건"
        )

        all_rows.extend(filtered)

        # 종료 조건: 응답이 page_size 미만이면 마지막
        if len(rows) < page_size:
            print(f"마지막 페이지(rows={len(rows)} < {page_size}) → 종료")
            break

        page_no += 1
        await asyncio.sleep(0.3)

    print(f"\n✅ 결제 대기 중 총 수집: {len(all_rows)}건")
    return all_rows

# =========================================
# ===== repayment (결제 처리) 순차 호출 =====
# =========================================
async def repay_orders(client: ZapClient, orders, sleep_sec=0.5):
    """
    각 주문에 대해 /operate/powerbank/repayment 호출.
    - POST body: {"order_no": "<주문번호>"}
    - 페이지 이동 없이 순차 호출. key는 call_operate_api()가 자동 갱신/재사용.
    - 반환: [{order_no, ok, status, json}, ...]
    """
    results = []
    total = len(orders)

    for i, order in enumerate(orders, 1):
        order_no = order.get("order_no")
        if not order_no:
            print(f"⚠️ [{i}/{total}] order_no 없음 → skip")
            continue

        print(f"\n💳 [{i}/{total}] repayment order_no={order_no}")

        try:
            res = await client.call_operate_api(
                "/operate/powerbank/repayment",
                method="POST",
                body={"order_no": order_no},
                max_retry=3,
            )
        except Exception as e:
            print(f"❌ [{i}/{total}] {order_no} 호출 예외: {e}")
            results.append({
                "order_no": order_no,
                "ok": False,
                "status": None,
                "json": {"error": str(e)},
            })
            continue

        status = res.get("status")
        js = res.get("json") or {}
        ok = (status == 200 and isinstance(js, dict) and js.get("code") == 1)

        print(
            f"   → {'✅ 성공' if ok else '⚠️ 실패'} "
            f"status={status} json={json.dumps(js, ensure_ascii=False)[:200]}"
        )

        results.append({
            "order_no": order_no,
            "ok": ok,
            "status": status,
            "json": js,
        })

        await asyncio.sleep(sleep_sec)

    ok_cnt = sum(1 for r in results if r["ok"])
    print(f"\n✅ repayment 완료: 성공 {ok_cnt} / 시도 {len(results)}")
    return results
# =========================================
# ===== 메인 =====
# =========================================
async def main():
    client = ZapClient()
    try:
        await client.ensure_login()

        # 1) 결제대기중 수집
        rows = await fetch_all_unfinished_orders(client)

        # 2) JSON 저장
        with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(f"💾 저장: {OUTPUT_JSON} ({len(rows)}건)")

        if not rows:
            print("결제대기중 주문 없음 → 종료")
            return

        # 3) repayment 순차 호출
        results = await repay_orders(client, rows, sleep_sec=0.5)

        # 4) 결과 저장
        result_path = OUTPUT_JSON.replace(".json", "_repay_results.json")
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"💾 repayment 결과 저장: {result_path}")

        # ========================================
        # ★ 여기에 결과 후처리 로직 추가 ★
        # ========================================

    finally:
        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("중단됨")
        sys.exit(0)
