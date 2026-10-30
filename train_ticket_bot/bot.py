# -*- coding: utf-8 -*-
"""
12306 自动订票核心引擎（基于 Playwright 驱动本机 Edge 浏览器）。

流程：
  1. 打开本机 Edge（可见窗口），进入 12306；
  2. 扫码登录；
  3. 按「出发站/到达站/日期」持续查询余票（内部走 queryG 接口，快速轮询）；
  4. 命中目标车次+席别后，回到页面点「预订」→ 确认乘车人 → 提交订单；
  5. 提交成功（到达支付页前）停止，语音+通知提示手动支付。

本模块只负责逻辑，Web 界面在 app.py。
"""
import asyncio
import json
import os
import subprocess
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright

import stations

BASE = os.path.dirname(os.path.abspath(__file__))


def _config_dir():
    """隐私/运行数据目录：默认放在源码目录之外（../config），可用环境变量 TICKET_CONFIG_DIR 覆盖。"""
    d = os.environ.get("TICKET_CONFIG_DIR")
    if d:
        return os.path.abspath(d)
    return os.path.abspath(os.path.join(BASE, "..", "config"))


CONFIG_DIR = _config_dir()
os.makedirs(CONFIG_DIR, exist_ok=True)

PROFILE = os.path.join(CONFIG_DIR, "browser_profile")
LOGIN_URL = "https://kyfw.12306.cn/otn/resources/login.html"
LEFT_URL = "https://kyfw.12306.cn/otn/leftTicket/init"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 席别 -> queryG 结果字段下标（0 起，已从 12306 前端 JS 验证）
SEAT_INDEX = {
    "商务座": 32,
    "特等座": 25,
    "一等座": 31,
    "二等座": 30,
    "高级软卧": 21,
    "软卧": 23,
    "硬卧": 28,
    "软座": 24,
    "硬座": 29,
    "无座": 26,
    "其他": 22,
}
# 席别 -> 页面表格单元格 id 前缀
SEAT_DOM = {
    "商务座": "SWZ",
    "特等座": "TZ",
    "一等座": "ZY",
    "二等座": "ZE",
    "高级软卧": "GR",
    "软卧": "RW",
    "硬卧": "YW",
    "软座": "RZ",
    "硬座": "YZ",
    "无座": "WZ",
    "其他": "QT",
}

# ---------- 全局日志 / 状态 ----------
log_buffer = deque(maxlen=1200)
log_lock = threading.Lock()

_state_lock = threading.Lock()
_state = {
    "status": "idle",          # idle / logging_in / logged_in / monitoring / booking / success / error / stopped
    "message": "就绪",
    "logged_in": False,
}


def now_str():
    return datetime.now().strftime("%H:%M:%S")


def log(msg, important=False):
    line = "[{}] {}".format(now_str(), msg)
    with log_lock:
        log_buffer.append(line)
    print(line, flush=True)
    if important:
        _announce(msg)


def _announce(msg):
    """macOS 语音 + 系统通知（尽力而为，失败不影响主流程）。"""
    try:
        subprocess.Popen(["say", msg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    try:
        subprocess.Popen(
            ["osascript", "-e",
             'display notification "{}" with title "12306自动订票"'.format(msg.replace('"', "'"))],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def get_logs():
    with log_lock:
        return list(log_buffer)


def set_state(**kw):
    with _state_lock:
        _state.update(kw)


def get_state():
    with _state_lock:
        return dict(_state)


def seat_available(value):
    """判断某席别是否可订：'有' 或数字为可订；'' / '--' / '无' / '0' 为不可订。"""
    if value is None:
        return False
    v = str(value).strip()
    if v in ("", "--", "无", "0", "＊", "*"):
        return False
    return True


def parse_result(result_str):
    """把 queryG 返回的单条 result 解析成 dict。"""
    p = result_str.split("|")
    if len(p) < 37:
        return None
    def g(i):
        return p[i] if len(p) > i else ""

    seats = {}
    for name, idx in SEAT_INDEX.items():
        seats[name] = g(idx)

    return {
        "train_no": g(2),          # 列车编号，用于定位表格行
        "code": g(3),              # 车次，如 K599
        "from_code": g(6),
        "to_code": g(7),
        "from_station": stations.code_to_name(g(6)),
        "to_station": stations.code_to_name(g(7)),
        "start_time": g(8),
        "arrive_time": g(9),
        "duration": g(10),
        "from_no": g(16),
        "to_no": g(17),
        "houbu": g(37),            # 候补标志
        "sale_time": g(55),        # 起售时间，如 202610040800
        "seats": seats,
    }


# ---------- 路线解析 ----------
def resolve_routes(config):
    """解析 routes 配置为 [(from_code, to_code, from_name, to_name), ...]，站名无效的跳过。"""
    routes_cfg = config.get("routes") or []
    out = []
    for r in routes_cfg:
        f = (r.get("from") or "").strip()
        t = (r.get("to") or "").strip()
        fc = stations.name_to_code(f)
        tc = stations.name_to_code(t)
        if not fc:
            log("无法识别出发站: {}".format(f or r))
            continue
        if not tc:
            log("无法识别到达站: {}".format(t or r))
            continue
        out.append((fc, tc, f, t))
    return out


# ---------- 时间校准 ----------
def server_time_offset():
    """估算本机与北京时间(服务器时间)的偏差：正数表示服务器更快。"""
    hosts = ["https://kyfw.12306.cn/otn/", "https://www.baidu.com/", "https://www.taobao.com/"]
    offsets = []
    for u in hosts:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=5) as r:
                date_str = r.headers.get("Date")
            if date_str:
                server = datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %Z")
                server = server.replace(tzinfo=timezone.utc)
                local = datetime.now(timezone.utc)
                offsets.append((server - local).total_seconds())
        except Exception:
            pass
    if offsets:
        offsets.sort()
        return offsets[len(offsets) // 2]
    return 0.0


# ---------- 核心引擎 ----------
class BookingBot:
    def __init__(self):
        self.pw = None
        self.ctx = None
        self.page = None
        self._stop = threading.Event()
        self._loop = None
        self._logging_in = False

    # ---- 启动 / 停止 ----
    async def _ensure_launched(self):
        if self.page is not None:
            return
        # 清理可能残留的单例锁（防止上次异常退出导致 Edge 无法再次启动）
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            p = os.path.join(PROFILE, name)
            try:
                if os.path.islink(p) or os.path.exists(p):
                    os.unlink(p)
            except Exception:
                pass
        self.pw = await async_playwright().start()
        headless = os.environ.get("HEADLESS") == "1"
        self.ctx = await self.pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE,
            channel="msedge",
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1380, "height": 900},
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        log("已启动本机 Edge 浏览器窗口（请勿关闭）")

    async def ensure_page(self, url=LEFT_URL):
        await self._ensure_launched()
        if self.page.url != url:
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log("打开页面失败: {}".format(e))
        return self.page

    async def close(self):
        try:
            if self.ctx:
                await self.ctx.close()
        except Exception:
            pass
        try:
            if self.pw:
                await self.pw.stop()
        except Exception:
            pass
        self.page = None
        self.ctx = None
        self.pw = None

    def stop(self):
        self._stop.set()

    # ---- 登录状态 ----
    async def _is_logged_in(self):
        if self.page is None:
            return False
        try:
            r = await self.page.evaluate("""
            async () => {
              const res = await fetch('/otn/login/checkUser', {
                method:'POST', credentials:'include',
                headers:{'Cache-Control':'no-cache','If-Modified-Since':'0'}
              });
              try { return await res.json(); } catch(e) { return {}; }
            }
            """)
            return bool(r and r.get("data") and r["data"].get("flag"))
        except Exception:
            return False

    async def login(self):
        """打开登录页等待扫码，成功后返回 True。"""
        await self._ensure_launched()
        self._logging_in = True
        try:
            set_state(status="logging_in", message="请扫码登录", logged_in=False)
            await self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            # 确保二维码在显示（有些环境默认显示账号登录，点一下「扫码登录」）
            try:
                scan = self.page.locator("text=扫码登录").first
                if await scan.count() > 0:
                    await scan.click(timeout=3000)
            except Exception:
                pass
            log("请在 Edge 窗口中用 12306 App 扫码登录", important=True)

            deadline = time.monotonic() + 300  # 最多等 5 分钟
            last_qr_refresh = time.monotonic()
            while not self._stop.is_set():
                if await self._is_logged_in():
                    log("登录成功")
                    set_state(logged_in=True, status="logged_in", message="已登录")
                    await self.page.goto(LEFT_URL, wait_until="domcontentloaded", timeout=60000)
                    return True
                now = time.monotonic()
                if now - last_qr_refresh > 120:
                    # 二维码过期，刷新页面换新码
                    log("二维码可能已过期，刷新登录页")
                    await self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                    last_qr_refresh = now
                if now > deadline:
                    log("等待扫码超时（5 分钟）")
                    break
                await asyncio.sleep(2)
            return False
        finally:
            self._logging_in = False

    # ---- 乘车人 ----
    async def get_passengers(self):
        await self.ensure_page()
        if not await self._is_logged_in():
            return {"ok": False, "error": "尚未登录"}
        try:
            data = await self.page.evaluate("""
            async () => {
              const res = await fetch('/otn/confirmPassenger/getPassengerDTOs', {
                method:'POST', credentials:'include'
              });
              try { return await res.json(); } catch(e) { return {}; }
            }
            """)
            normal = data.get("data", {}).get("normal_passengers", []) or []
            result = []
            for p in normal:
                result.append({
                    "name": p.get("passenger_name"),
                    "id_no": p.get("passenger_id_no"),
                    "type": p.get("passenger_type"),
                    "type_name": p.get("passenger_type_name"),
                })
            return {"ok": True, "passengers": result}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- 余票查询 ----
    async def _fetch_query_json(self, from_code, to_code, date):
        js = """
        async (args) => {
          const url = '/otn/leftTicket/queryG?leftTicketDTO.train_date=' + args.date
                    + '&leftTicketDTO.from_station=' + args.from_code
                    + '&leftTicketDTO.to_station=' + args.to_code
                    + '&purpose_codes=ADULT';
          const res = await fetch(url, {credentials:'include'});
          if (res.status !== 200) return {httpstatus: res.status, raw: (await res.text()).slice(0,200)};
          try { return await res.json(); } catch(e) { return {httpstatus: res.status, raw: (await res.text()).slice(0,200)}; }
        }
        """
        return await self.page.evaluate(js, {"date": date, "from_code": from_code, "to_code": to_code})

    async def query_trains(self, routes, date):
        """routes: [(from_code, to_code), ...]，按城市分组查询并做精确站码过滤。"""
        if self.page is None or "leftTicket" not in self.page.url:
            await self.ensure_page()
        if not routes:
            return {"ok": False, "error": "未指定路线"}

        # 按 (出发城市, 到达城市) 分组，每组用第一对站码做代表查一次
        groups = {}
        for fc, tc in routes:
            fcity = stations.code_to_city(fc) or fc
            tcity = stations.code_to_city(tc) or tc
            groups.setdefault((fcity, tcity), (fc, tc))

        allowed = set(routes)   # 精确站码对
        merged = {}
        errors = []
        for (fcity, tcity), (fc, tc) in groups.items():
            data = await self._fetch_query_json(fc, tc, date)
            if not isinstance(data, dict) or data.get("httpstatus") != 200 or not data.get("data"):
                errors.append("{}-{}: {}".format(fc, tc, str(data.get("raw") or data.get("messages") or "无数据")[:80]))
                continue
            for r in data["data"].get("result") or []:
                t = parse_result(r)
                if not t:
                    continue
                key = (t["train_no"], t["from_no"], t["to_no"])
                if key in merged:
                    continue
                if (t["from_code"], t["to_code"]) in allowed:
                    merged[key] = t

        if not merged and errors:
            return {"ok": False, "error": "；".join(errors)}
        trains = sorted(merged.values(), key=lambda t: (t["start_time"], t["code"]))
        return {"ok": True, "trains": trains}

    # ---- 页面渲染 + 预订入口 ----
    async def _render_query(self, from_code, from_name, to_code, to_name, date, train_no=None):
        page = await self.ensure_page()
        await page.evaluate(
            """
            (args) => {
              const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
              set('fromStation', args.from_code);
              set('fromStationText', args.from_name);
              set('toStation', args.to_code);
              set('toStationText', args.to_name);
              set('train_date', args.date);
              document.cookie = '_jc_save_fromStation=' + encodeURIComponent(args.from_name) + ',' + args.from_code + '; path=/';
              document.cookie = '_jc_save_toStation=' + encodeURIComponent(args.to_name) + ',' + args.to_code + '; path=/';
              document.cookie = '_jc_save_fromDate=' + args.date + '; path=/';
              document.cookie = '_jc_save_toDate=' + args.date + '; path=/';
              document.cookie = '_jc_save_wfdc_flag=dc; path=/';
            }
            """,
            {"from_code": from_code, "from_name": from_name,
             "to_code": to_code, "to_name": to_name, "date": date},
        )
        try:
            await page.click("#query_ticket", timeout=5000)
        except Exception:
            await page.evaluate("document.getElementById('query_ticket').click()")
        # 等待表格出现目标车次
        if train_no:
            try:
                await page.wait_for_selector(
                    'tr[id^="ticket_{}_"]'.format(train_no), timeout=15000)
            except Exception as e:
                log("渲染车次表格超时: {}".format(e))
        else:
            await page.wait_for_timeout(2500)

    async def _click_book(self, train):
        """点目标车次的「预订」按钮。"""
        page = self.page
        row = page.locator('tr[id^="ticket_{}_"]'.format(train["train_no"])).first
        if await row.count() == 0:
            return False
        link = row.locator('a:has-text("预订")').first
        if await link.count() == 0:
            return False
        log("点击 {} 预订按钮".format(train["code"]))
        try:
            await link.click(timeout=5000)
        except Exception as e:
            log("点击预订按钮异常: {}".format(e))
            return False
        # 自动点掉可能出现的「温馨提示」弹窗
        await self._dismiss_tips(timeout=6)
        # 等待跳转到确认页
        try:
            await page.wait_for_url("**/confirmPassenger/**", timeout=15000)
            return True
        except Exception:
            return False

    # ---- 确认页：选乘车人 + 提交 ----
    async def _confirm_and_submit(self, passenger_name, seat_name):
        page = self.page
        log("进入乘车人确认页")
        await page.wait_for_timeout(800)

        # 1) 勾选乘车人
        ok = await self._select_passenger(passenger_name)
        if not ok:
            log("未自动选中乘车人，请在 Edge 窗口手动勾选后，程序将继续", important=True)

        # 2) 若弹出「选铺/选座」弹窗，尝试关闭（不选则随机分配）
        await self._close_seat_dialog()

        # 3) 提交订单
        try:
            await page.click("#submitOrder_id", timeout=8000)
        except Exception:
            # 尝试其它可能的提交按钮
            for sel in ["a:has-text('提交订单')", "input:has-text('提交订单')", "button:has-text('提交订单')"]:
                try:
                    await page.click(sel, timeout=2000)
                    break
                except Exception:
                    continue
        log("已提交订单，等待处理…")

        # 3.5) 处理「请核对一下信息」选座/选铺弹窗：选座选 D，选铺跳过（随机铺位）
        await self._handle_seat_selection(seat_name)
        await self._handle_bed_selection()

        # 3.6) 点掉可能出现的「温馨提示」弹窗
        await self._dismiss_tips(timeout=2)

        # 4) 处理可能出现的验证码（滑块/点选），交由用户手动完成后继续
        await self._handle_captcha()

        # 5) 等待结果：排队 -> 出票 -> 跳转支付页
        return await self._wait_for_result()

    async def _select_passenger(self, name):
        page = self.page
        strategies = [
            '#normal_passenger_id li:has-text("{}") input[type=checkbox]'.format(name),
            '#normal_passenger_id input[type=checkbox] ~ *:has-text("{}")'.format(name),
        ]
        for sel in strategies:
            try:
                cb = page.locator(sel).first
                if await cb.count() > 0:
                    if not await cb.is_checked():
                        await cb.check(timeout=3000)
                    log("已勾选乘车人: {}".format(name))
                    return True
            except Exception:
                continue
        # 兜底：找包含姓名的 li，勾选其中 checkbox
        try:
            lis = page.locator("#normal_passenger_id li")
            n = await lis.count()
            for i in range(n):
                li = lis.nth(i)
                txt = await li.inner_text()
                if name and name in txt:
                    cb = li.locator("input[type=checkbox]").first
                    if await cb.count() > 0:
                        if not await cb.is_checked():
                            await cb.check(timeout=3000)
                        log("已勾选乘车人: {}".format(name))
                        return True
        except Exception:
            pass
        return False

    async def _close_seat_dialog(self):
        page = self.page
        for sel in ['.dhtmlx_window .dhtmlx_wins_btns', 'a:has-text("关闭")', 'a:has-text("确定")',
                    'a:has-text("确认")', '.dhx_modal_close']:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click(timeout=2000)
                    await page.wait_for_timeout(500)
                    return
            except Exception:
                continue

    async def _dump_seat_dialog(self):
        """把选座弹窗 DOM 结构 dump 到文件，便于排查。"""
        try:
            info = await self.page.evaluate("""
            () => {
              const grab = (sel) => { const e = document.querySelector(sel); return e ? e.outerHTML.slice(0,6000) : null; };
              const vis = (e) => e && e.offsetParent !== null;
              return {
                seat_sel: grab('#id-seat-sel'),
                dhtmlx_win: grab('.dhtmlx_window'),
                content_auto: grab('#content_autosubmitcheckticketinfo'),
                buttons: Array.from(document.querySelectorAll('a,button,input[type=button],input[type=submit]'))
                  .filter(vis).map(e => ({tag:e.tagName, id:e.id, cls:(e.className||'').toString(), text:(e.innerText||e.value||'').trim().slice(0,24)}))
                  .filter(b => b.text || b.id).slice(0, 50),
                checkboxes: Array.from(document.querySelectorAll('input[type=checkbox]'))
                  .filter(vis).map(e => { const p = e.closest('label')||e.parentElement; return {id:e.id, name:e.name, checked:e.checked, near:(p?p.innerText:'').trim().slice(0,40)}; })
                  .slice(0, 30),
                jing_nodes: Array.from(document.querySelectorAll('*'))
                  .filter(e => e.children.length===0 && (e.innerText||'').indexOf('静音')>=0)
                  .map(e => ({tag:e.tagName, id:e.id, cls:(e.className||'').toString(), text:(e.innerText||'').trim().slice(0,30)}))
                  .slice(0, 20),
              };
            }
            """)
            path = os.path.join(CONFIG_DIR, "seat_dialog_dump.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(info, ensure_ascii=False, indent=1))
            log("选座弹窗结构已 dump 到 {}".format(path))
        except Exception as e:
            log("dump 选座弹窗失败: {}".format(e))

    async def _js_click_id(self, eid):
        """用 JS 直接触发元素 click，绕过遮挡/pointer-events/禁用等 actionability 限制。"""
        try:
            ok = await self.page.evaluate(
                "(id) => { const e = document.getElementById(id); if(e){ e.click(); return true; } return false; }",
                eid)
            return bool(ok)
        except Exception:
            return False

    async def _handle_seat_selection(self, seat_name, seat_letter="D"):
        """处理「请核对一下信息」选座弹窗：只选 D 座，然后点确认（不勾静音车厢，避免触发温馨提示）。"""
        page = self.page
        try:
            await page.wait_for_selector("#id-seat-sel", state="visible", timeout=4000)
        except Exception:
            return  # 无选座弹窗（卧铺/不支持选座），继续
        log("检测到选座弹窗，自动选座")
        await self._dump_seat_dialog()
        if await self._click_seat_letter(seat_letter):
            log("已选 {} 座".format(seat_letter))
        else:
            log("未自动选中 {} 座，仍继续提交".format(seat_letter), important=True)
        await page.wait_for_timeout(200)
        # 点确认：优先 JS 直接点击（绕过遮挡/禁用判断）
        if await self._js_click_id("qr_submit_id"):
            log("已点确认按钮: #qr_submit_id")
            await page.wait_for_timeout(300)
            return
        # 兜底 force 点击
        for sel in ['a:has-text("确认")', 'a:has-text("确定")']:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    await btn.click(force=True, timeout=3000)
                    log("已点确认按钮(force): {}".format(sel))
                    return
            except Exception:
                continue
        log("未找到可用的确认按钮，请查看 {}".format(os.path.join(CONFIG_DIR, "seat_dialog_dump.json")), important=True)

    async def _handle_bed_selection(self):
        """卧铺选铺弹窗：不选具体铺位（随机分配），直接点确认。"""
        page = self.page
        try:
            await page.wait_for_selector("#id-bed-sel", state="visible", timeout=4000)
        except Exception:
            return  # 无选铺弹窗
        log("检测到选铺弹窗，跳过选铺（随机铺位）")
        await self._dump_seat_dialog()
        await page.wait_for_timeout(200)
        if await self._js_click_id("qr_submit_id"):
            log("已点确认按钮: #qr_submit_id")
            await page.wait_for_timeout(300)
            return
        for sel in ['a:has-text("确认")', 'a:has-text("确定")']:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    await btn.click(force=True, timeout=3000)
                    log("已点确认按钮(force): {}".format(sel))
                    return
            except Exception:
                continue
        log("选铺弹窗未找到确认按钮，请查看 {}".format(os.path.join(CONFIG_DIR, "seat_dialog_dump.json")), important=True)

    async def _click_seat_letter(self, letter):
        """点击选座区域里指定字母的座位。"""
        page = self.page
        for sel in [
            '#id-seat-sel a[id="1{}"]'.format(letter),
            '#erdeng1 a[id$="{}"]'.format(letter),
            '#id-seat-sel a[id$="{}"]'.format(letter),
            '#erdeng1 a:text-is("{}")'.format(letter),
            '#id-seat-sel a:text-is("{}")'.format(letter),
            '#id-seat-sel a:has-text("{}")'.format(letter),
        ]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click(timeout=2000)
                    return True
            except Exception:
                continue
        return False

    async def _check_quiet_car(self):
        """勾选静音车厢（找不到或该车次无此选项则跳过，不影响下单）。"""
        page = self.page
        # 0) 已知 id：seat-jy
        try:
            cb = page.locator('#seat-jy').first
            if await cb.count() > 0 and await cb.is_visible():
                if not await cb.is_checked():
                    await cb.check(timeout=2000)
                log("已勾选静音车厢")
                return
        except Exception:
            pass
        # 1) 遍历可见 checkbox，按 id/name/附近文本匹配“静音”
        try:
            cbs = page.locator("input[type=checkbox]")
            n = await cbs.count()
            for i in range(n):
                cb = cbs.nth(i)
                try:
                    if not await cb.is_visible():
                        continue
                    near = await cb.evaluate(
                        "el => { const p = el.closest('label') || el.parentElement; "
                        "return (p ? p.innerText : '') + '|' + (el.id || '') + '|' + (el.name || ''); }")
                    low = near.lower()
                    if ("静音" in near) or ("jing" in low) or ("quiet" in low) or ("silent" in low):
                        if not await cb.is_checked():
                            await cb.check(timeout=2000)
                        log("已勾选静音车厢")
                        return
                except Exception:
                    continue
        except Exception:
            pass
        # 2) 兜底：点含“静音”的 label/span（可能是非 checkbox 的开关）
        for sel in ['label:has-text("静音")', 'span:has-text("静音")', 'a:has-text("静音")']:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click(timeout=2000)
                    log("已点击静音车厢选项")
                    return
            except Exception:
                continue
        log("未找到静音车厢选项（可能该车次无静音车厢）")

    async def _dismiss_tips(self, timeout=5):
        """自动点掉「温馨提示/提示」类弹窗的「确定」按钮（循环直到消失或超时）。"""
        page = self.page
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            clicked = False
            for sel in ['.dhx_modal_alert a:has-text("确定")',
                        '.dhtmlx_window a:has-text("确定")',
                        '.dhx_modal a:has-text("确定")',
                        'a:has-text("确定")',
                        'button:has-text("确定")',
                        'input[value="确定"]']:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0:
                        await btn.click(force=True, timeout=2000)
                        log("已点掉提示弹窗「确定」")
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                return
            await asyncio.sleep(0.4)

    async def _handle_captcha(self):
        """检测验证码，出现则暂停并提示用户手动完成。"""
        page = self.page
        await page.wait_for_timeout(1500)
        markers = ["#nc_1_n1t", "#nc_1__scale_text", "iframe[src*='captcha']",
                   "#randCode", ".nc-container", "#login-box .touclick-image"]
        found = False
        for m in markers:
            try:
                el = page.locator(m).first
                if await el.count() > 0 and await el.is_visible():
                    found = True
                    break
            except Exception:
                continue
        if found:
            log("检测到验证码，请在 Edge 窗口手动完成滑块/点选，完成后程序自动继续", important=True)
            # 等待验证码区域消失（最多等 90 秒）
            try:
                await page.wait_for_function(
                    "() => { const e = document.querySelector('#nc_1_n1t, #randCode'); "
                    "return !e || e.offsetParent === null; }",
                    timeout=90000)
                log("验证码已完成，继续")
            except Exception:
                log("等待验证码超时，继续尝试")

    async def _dump_page_text(self, tag="timeout"):
        """把当前页面文本 dump 到文件，便于排查超时原因。"""
        try:
            txt = await self.page.evaluate("document.body ? document.body.innerText.slice(0,3000) : ''")
            url = self.page.url
            path = os.path.join(CONFIG_DIR, "page_text_{}.txt".format(tag))
            with open(path, "w", encoding="utf-8") as f:
                f.write("URL: {}\n\n{}".format(url, txt))
            log("页面文本已 dump 到 {}".format(path))
        except Exception:
            pass

    async def _wait_for_result(self, max_idle_seconds=30, hard_timeout=600):
        """等待出票结果：排队时持续等待；识别出票失败立即返回 False；成功返回 True。"""
        fail_keys = ("出票失败", "余票不足", "没有足够的余票", "购票失败", "下单失败", "订票失败",
                     "当前排队人数过多", "已售罄", "无票", "出票超时", "排队超时", "无法预订")
        page = self.page
        start = time.monotonic()
        last_busy = time.monotonic()   # 最近一次看到「排队/处理中」的时间
        last_qlog = 0.0
        while True:
            if self._stop.is_set():
                return False
            if time.monotonic() - start > hard_timeout:
                log("等待出票结果超过 {} 秒，判定超时".format(hard_timeout))
                await self._dump_page_text("hard_timeout")
                return False
            # 点掉可能出现的提示弹窗，加速流程
            await self._dismiss_tips(timeout=1)
            url = page.url
            if "pay" in url or "payOrder" in url:
                log("✅ 订单提交成功，已跳转支付页")
                return True
            # 页面文案判断
            try:
                txt = await page.evaluate("document.body ? document.body.innerText.slice(0,4000) : ''")
                # 成功：席位已锁定 / 支付页（支付剩余时间、网上支付按钮）
                success_keys = ("席位已锁定", "支付剩余时间", "网上支付", "订单提交成功")
                if any(k in txt for k in success_keys) or ("支付" in txt and "订单号" in txt):
                    log("✅ 订票成功（席位已锁定），请尽快手动支付", important=True)
                    return True
                # 出票失败：立即返回，交给外层重新监控抢票
                if any(k in txt for k in fail_keys):
                    log("❌ 出票失败（页面提示无票/失败），返回重试")
                    await self._dump_page_text("fail_detected")
                    return False
                if any(k in txt for k in ("排队", "处理中", "正在处理", "订单已经提交")):
                    last_busy = time.monotonic()
                    if time.monotonic() - last_qlog > 5:
                        log("订单处理中，等待出票…")
                        last_qlog = time.monotonic()
            except Exception:
                pass
            # 长时间既没排队也没结果 → 超时
            if time.monotonic() - last_busy > max_idle_seconds:
                log("等待 {} 秒无进展，判定超时".format(max_idle_seconds))
                await self._dump_page_text("idle_timeout")
                return False
            await asyncio.sleep(1)

    async def _submit_waitlist(self, train, seat, date):
        """订单失败后，给该车次+席别提交候补（尽力而为，失败不阻断主流程）。"""
        page = self.page
        seat_prefix = SEAT_DOM.get(seat, "ZE")
        try:
            # 回到余票页并渲染该车次
            await self._render_query(train["from_code"], train["from_station"],
                                     train["to_code"], train["to_station"], date, train["train_no"])
            # 点击「候补」单元格
            clicked = await page.evaluate(
                """
                (args) => {
                  const train_no = args.train_no, seat = args.seat_prefix + '_';
                  const tds = document.querySelectorAll('td[hbid]');
                  for (const td of tds) {
                    const hbid = td.getAttribute('hbid') || '';
                    if (hbid.indexOf(train_no) >= 0 && hbid.indexOf(seat) >= 0 && (td.textContent || '').indexOf('候补') >= 0) { td.click(); return true; }
                  }
                  for (const td of tds) {
                    const hbid = td.getAttribute('hbid') || '';
                    if (hbid.indexOf(train_no) >= 0 && (td.textContent || '').indexOf('候补') >= 0) { td.click(); return true; }
                  }
                  return false;
                }
                """,
                {"train_no": train["train_no"], "seat_prefix": seat_prefix},
            )
            if not clicked:
                log("未找到 {} {} 的候补入口，跳过候补".format(train["code"], seat))
                return False
            log("已点击 {} {} 候补入口".format(train["code"], seat))
            await page.wait_for_timeout(1000)
            await self._dump_page_text("waitlist")
            # 点掉可能的确认/提示弹窗
            await self._dismiss_tips(timeout=3)
            # 尝试点候补提交按钮
            if await self._js_click_id("hbSubmit"):
                log("已点候补提交按钮 #hbSubmit")
                await page.wait_for_timeout(1000)
                await self._dump_page_text("waitlist_submit")
            return True
        except Exception as e:
            log("候补提交异常: {}".format(e))
            return False

    # ---- 主流程：定时 + 监控 + 下单 ----
    async def run(self, config):
        self._stop.clear()
        try:
            routes = resolve_routes(config)
            date = (config.get("date") or "").strip()
            train_numbers = [t.strip().upper() for t in config.get("train_numbers", []) if t and t.strip()]
            seat_priority = [s for s in config.get("seat_priority", []) if s in SEAT_INDEX]
            passenger = (config.get("passenger") or "").strip()
            start_time = (config.get("start_time") or "").strip()

            if not routes:
                log("未填写有效路线（出发站/到达站）", important=True)
                set_state(status="error", message="未填写有效路线")
                return
            route_codes = [(fc, tc) for fc, tc, _f, _t in routes]
            route_text = "、".join("{}→{}".format(f, t) for _fc, _tc, f, t in routes)
            if not date:
                log("请填写乘车日期", important=True)
                set_state(status="error", message="请填写乘车日期")
                return
            if not passenger:
                log("请填写乘车人姓名", important=True)
                set_state(status="error", message="请填写乘车人姓名")
                return
            if not seat_priority:
                seat_priority = ["二等座", "硬卧", "硬座", "软卧"]

            await self.ensure_page()

            # 定时抢票：开售临近却未登录时，强烈提醒先登录
            if start_time and not await self._is_logged_in():
                try:
                    h, m = start_time.split(":")
                    target = datetime.now().replace(hour=int(h), minute=int(m), second=0, microsecond=0)
                    gap = (target - datetime.now()).total_seconds()
                    if 0 <= gap < 180:
                        log("⚠️ 距开售 {} 不足 3 分钟且尚未登录！请立即扫码登录，否则会错过开售", important=True)
                except Exception:
                    pass

            # 登录
            if not await self._is_logged_in():
                if self._logging_in:
                    log("登录进行中，等待扫码完成…")
                    while not self._stop.is_set() and not await self._is_logged_in():
                        await asyncio.sleep(1)
                else:
                    log("未登录，先扫码登录…")
                    if not await self.login():
                        log("登录未完成，停止", important=True)
                        set_state(status="error", message="登录未完成")
                        return
                if not await self._is_logged_in():
                    log("登录未完成，停止", important=True)
                    set_state(status="error", message="登录未完成")
                    return

            # 定时等待
            if start_time:
                await self._wait_until(start_time)

            set_state(status="monitoring",
                      message="监控中：{} {} 车次[{}] 席别[{}]".format(
                          route_text, date,
                          "、".join(train_numbers) or "任意", "、".join(seat_priority)))
            log("开始抢票：{}  {}  车次[{}] 席别[{}]".format(
                route_text, date,
                "、".join(train_numbers) or "任意", "、".join(seat_priority)), important=True)

            poll = float(config.get("poll_interval", 0.8) or 0.8)
            heartbeat = 0
            gave_up = set()   # 已候补/已放弃的车次，不再直接购买
            while not self._stop.is_set():
                heartbeat += 1
                res = await self.query_trains(route_codes, date)
                if not res.get("ok"):
                    log("查询失败: {}".format(res.get("error")))
                    await asyncio.sleep(2)
                    continue

                targets = res["trains"]
                if train_numbers:
                    targets = [t for t in targets if t["code"] in train_numbers]
                # 已候补/已放弃的车次不再直接购买
                targets = [t for t in targets if t["code"] not in gave_up]

                if heartbeat % 6 == 0:
                    summary = "；".join(
                        "{} {}".format(t["code"], " ".join(
                            "{}={}".format(s, t["seats"].get(s, "")) for s in seat_priority))
                        for t in targets[:5])
                    log("监控中… " + (summary or "(无目标车次)"))

                # 寻找可订的车次+席别（按优先级）
                picked = None
                for t in targets:
                    for s in seat_priority:
                        if seat_available(t["seats"].get(s)):
                            picked = (t, s)
                            break
                    if picked:
                        break

                if picked:
                    train, seat = picked
                    log("🎯 发现余票：{}  {}  出发 {}  席别【{}】={}，开始下单".format(
                        train["code"], train["from_station"], train["start_time"],
                        seat, train["seats"].get(seat)), important=True)
                    set_state(status="booking", message="正在下单 {} {}".format(train["code"], seat))
                    await self._render_query(train["from_code"], train["from_station"],
                                             train["to_code"], train["to_station"], date, train["train_no"])
                    clicked = await self._click_book(train)
                    if clicked:
                        ok = await self._confirm_and_submit(passenger, seat)
                        if ok:
                            set_state(status="success",
                                      message="订票成功，请手动支付")
                            log("🎉 订票成功！请在 12306 页面/App 手动完成支付", important=True)
                            return
                        else:
                            log("本次下单未成功，候补该车次并放弃直接购买")
                            gave_up.add(train["code"])
                            await self._submit_waitlist(train, seat, date)
                            set_state(status="monitoring", message="继续监控")
                            await self.ensure_page()
                    else:
                        log("点击预订失败，继续监控")
                await asyncio.sleep(poll)

            set_state(status="stopped", message="已停止")
            log("已停止")
        except Exception as e:
            import traceback
            log("运行异常: {}".format(e), important=True)
            log(traceback.format_exc())
            set_state(status="error", message="异常: {}".format(e))

    async def _wait_until(self, start_time):
        """定时等待：到点前 5 秒开始预热，到点立即抢。"""
        try:
            h, m = start_time.split(":")
            target = datetime.now().replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            if target <= datetime.now():
                log("开始时间 {} 已过，立即开始".format(start_time))
                return
        except Exception:
            log("开始时间格式无效，立即开始")
            return

        # 时间校准（对齐服务器时间）
        offset = server_time_offset()
        if abs(offset) > 0.3:
            log("已校准时间：本机与服务器偏差 {:.2f} 秒".format(offset))
        # 服务器时间 = 本机时间 + offset；要服务器到点，本机需在 target-offset 触发
        target_local = target - timedelta(seconds=offset)
        wait = (target_local - datetime.now()).total_seconds() - 5  # 提前 5 秒预热
        if wait > 0:
            log("将在 {} 准时开抢（提前 5 秒预热），请保持网络与浏览器开启".format(start_time), important=True)
            await asyncio.sleep(wait)
        # 预热：紧循环直到触发点
        while datetime.now() < target_local:
            await asyncio.sleep(0.02)
        log("⏰ 到点 {}，开始抢票！".format(datetime.now().strftime("%H:%M:%S.%f")), important=True)


# ---------- 线程封装：单一事件循环管理浏览器 ----------
_manager = None


class BotManager:
    """在独立线程持有唯一事件循环，所有 Playwright 协程都提交到该循环执行。"""

    def __init__(self):
        self.bot = BookingBot()
        self.loop = None
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return

        def runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop
            loop.run_forever()

        self.thread = threading.Thread(target=runner, name="bot-loop", daemon=True)
        self.thread.start()
        while self.loop is None:
            time.sleep(0.05)

    def _submit(self, coro, timeout=None):
        self.start()
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if timeout:
            return fut.result(timeout=timeout)
        return fut

    # 后台执行（不阻塞调用方）
    def login_async(self):
        self._submit(self.bot.login())

    def run_async(self, config):
        self._submit(self.bot.run(config))

    def stop(self):
        self.bot.stop()

    # 阻塞调用（供 Flask 查询使用）
    def query(self, routes, date, timeout=40):
        return self._submit(self.bot.query_trains(routes, date), timeout)

    def passengers(self, timeout=30):
        return self._submit(self.bot.get_passengers(), timeout)


def get_manager():
    global _manager
    if _manager is None:
        _manager = BotManager()
    return _manager

