#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py — 本地视频提取服务
================================
浏览器打开 http://127.0.0.1:8765 ，粘贴链接即可把视频存到本地。

零第三方依赖（标准库 + 本机 ffmpeg）。
"""
import hashlib
import http.server
import ipaddress
import json
import os
import pathlib
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# PyInstaller 打包后：资源在临时解包目录，用户数据要留在 exe 旁边
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    HERE = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, "_MEIPASS", HERE)
else:
    HERE = RES_DIR = os.path.dirname(os.path.abspath(__file__))


def _is_system_dir(p):
    """装在 Program Files / Windows 下时，数据必须挪走。
    不能只靠"是否可写"判断——管理员账户对 Program Files 也有写权限。"""
    if not p:
        return False
    try:
        parts = [x.lower() for x in pathlib.Path(p).parts]
    except Exception:
        return False
    low = "\\".join(parts)
    return ("program files" in parts
            or "program files (x86)" in parts
            or "programdata" in parts
            or (parts and parts[0].rstrip("\\") == "c:" and "windows" in parts))


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".wtest")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return True
    except Exception:
        return False


# 便携用法：数据放在程序目录；装到系统目录时改用 %LOCALAPPDATA%\VideoGrabber
if _is_system_dir(HERE) or not _writable(HERE):
    DATA_DIR = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "VideoGrabber")
    os.makedirs(DATA_DIR, exist_ok=True)
else:
    DATA_DIR = HERE

DEFAULT_OUT = os.path.join(DATA_DIR, "downloads")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
PORT = 8765
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# ============================================================ yt-dlp 通道
# 这些站点强制登录，未登录拿不到媒体地址
NEED_COOKIE_HOSTS = ("twitter.com", "x.com", "vimeo.com", "instagram.com",
                     "facebook.com", "fb.watch", "tiktok.com", "twitch.tv",
                     "y.qq.com",        # QQ 音乐未登录取不到播放地址
                     "bilibili.com")   # B 站登录后能拿 1080P
COOKIE_FILE = os.path.join(DATA_DIR, "cookies.txt")

# 需要登录的平台清单（key 对应前端展示顺序）
COOKIE_PLATFORMS = [
    ("bilibili", "B站", ("bilibili.com",), "登录后可下 1080P（未登录只有 480P）"),
    ("x", "X / Twitter", ("x.com", "twitter.com"), "强制登录，否则拿不到视频"),
    ("qqmusic", "QQ音乐", ("y.qq.com", "qq.com"), "未登录只给试听，取不到播放地址"),
    ("netease", "网易云音乐", ("music.163.com", "163.com"), "不登录也能下 128k"),
    ("vimeo", "Vimeo", ("vimeo.com",), "只对登录用户开放"),
    ("instagram", "Instagram", ("instagram.com",), "部分内容需要登录"),
    ("douyin", "抖音", ("douyin.com",), "一般不需要，风控时可试"),
    ("youtube", "YouTube", ("youtube.com",), "一般不需要"),
]


def _read_cookie_domains():
    """读 cookies.txt 里出现过的域名。"""
    if not os.path.isfile(COOKIE_FILE):
        return []
    out = set()
    try:
        with open(COOKIE_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    out.add(parts[0].lstrip(".").lower())
    except Exception:
        pass
    return sorted(out)


def cookie_status():
    domains = _read_cookie_domains()
    items = []
    for key, name, need, note in COOKIE_PLATFORMS:
        hit = any(any(d == n or d.endswith("." + n) for d in domains) for n in need)
        items.append({"key": key, "name": name, "ok": hit, "note": note})
    return {
        "has_file": os.path.isfile(COOKIE_FILE),
        "path": COOKIE_FILE,
        "count": len(domains),
        "domains": domains[:40],
        "platforms": items,
    }


def _clean_field(v):
    """Netscape 格式用 tab 分隔、换行分行，字段里必须去掉这些，
    否则可以伪造出额外的 cookie 行。"""
    return (v or "").replace("\t", "").replace("\r", "").replace("\n", "")


def _chrome_cookies_to_netscape(items):
    """浏览器扩展传来的 cookie 数组 → Netscape 格式。"""
    lines = ["# Netscape HTTP Cookie File"]
    for c in items:
        domain = _clean_field(c.get("domain")).strip()
        name = _clean_field(c.get("name")).strip()
        if not domain or not name:
            continue
        exp = c.get("expirationDate")
        try:
            exp = int(float(exp)) if exp else 9999999999
        except (TypeError, ValueError):
            exp = 9999999999
        lines.append("\t".join([
            domain,
            "FALSE" if c.get("hostOnly") else "TRUE",
            _clean_field(c.get("path")) or "/",
            "TRUE" if c.get("secure") else "FALSE",
            str(exp), name, _clean_field(c.get("value")),
        ]))
    return "\n".join(lines) + "\n"


def _valid_cookie_text(text):
    if "Netscape HTTP Cookie File" in text[:200]:
        return True
    for line in text.splitlines():
        if line.strip() and not line.startswith("#") and line.count("\t") >= 6:
            return True
    return False


# ---------- 平台快捷入口 ----------
PLATFORMS = [
    ("bilibili", "B站", "https://www.bilibili.com", ("bilibili.com", "b23.tv")),
    ("youtube", "YouTube", "https://www.youtube.com", ("youtube.com", "youtu.be")),
    ("douyin", "抖音", "https://www.douyin.com", ("douyin.com",)),
    ("xiaohongshu", "小红书", "https://www.xiaohongshu.com", ("xiaohongshu.com", "xhslink.com")),
    ("weibo", "微博", "https://weibo.com", ("weibo.com", "weibo.cn")),
    ("kuaishou", "快手", "https://www.kuaishou.com", ("kuaishou.com",)),
    ("x", "X", "https://x.com", ("x.com", "twitter.com")),
    ("tiktok", "TikTok", "https://www.tiktok.com", ("tiktok.com",)),
    ("netease", "网易云", "https://music.163.com", ("music.163.com",)),
    ("qqmusic", "QQ音乐", "https://y.qq.com", ("y.qq.com",)),
    ("instagram", "Instagram", "https://www.instagram.com", ("instagram.com",)),
    ("vimeo", "Vimeo", "https://vimeo.com", ("vimeo.com",)),
]


# 浏览器商店里的现成导出插件（一键安装，不需要开发者模式）
EXT_STORE_CHROME = ("https://chromewebstore.google.com/detail/"
                    "get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc")
EXT_STORE_EDGE = "https://microsoftedge.microsoft.com/addons/search/cookies.txt"
EXT_STORE_SEARCH = "https://chromewebstore.google.com/search/cookies.txt"


def ensure_extension_dir():
    """把打包在 exe 里的扩展释放到程序目录旁边，返回路径。"""
    dst = os.path.join(DATA_DIR, "extension")
    if os.path.isfile(os.path.join(dst, "manifest.json")):
        return dst
    src = os.path.join(RES_DIR, "extension")
    if not os.path.isfile(os.path.join(src, "manifest.json")):
        return None
    try:
        os.makedirs(dst, exist_ok=True)
        for fn in ("manifest.json", "popup.html", "popup.js"):
            s = os.path.join(src, fn)
            if os.path.isfile(s):
                shutil.copy2(s, os.path.join(dst, fn))
        return dst
    except Exception:
        return None


def open_extension_page():
    """打开浏览器的扩展管理页，方便用户加载。"""
    exe = _find_browser_exe("Chrome") or _find_browser_exe("Edge")
    if not exe:
        return False
    try:
        subprocess.Popen([exe, "chrome://extensions/"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def read_clipboard_text():
    """读 Windows 剪贴板里的文本。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        CF_UNICODETEXT = 13
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.OpenClipboard(None):
            return None
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return None
            ptr = kernel32.GlobalLock(wintypes.HGLOBAL(h))
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(wintypes.HGLOBAL(h))
        finally:
            user32.CloseClipboard()
    except Exception:
        return None


def open_in_browser(url):
    """用系统默认浏览器打开。"""
    try:
        if sys.platform == "win32":
            os.startfile(url)          # noqa: S606  (本地工具，链接来自白名单)
        else:
            import webbrowser
            webbrowser.open(url)
        return True
    except Exception:
        return False


# ---------- 专用浏览器（cookie 来源）----------
# Chrome 137+ 禁止在默认用户目录开调试端口，只能用独立目录。
# 好处是不碰用户日常浏览器，坏处是首次要在里面登录一次。
BROWSER_PROFILE = os.path.join(DATA_DIR, "browser-profile")
CDP_PORT = 9333


def _cdp_alive():
    return _cdp_port_ready(CDP_PORT)


def ensure_login_browser(url=None):
    """启动或复用专用浏览器。返回 (ok, message)。"""
    exe = _find_browser_exe("Chrome") or _find_browser_exe("Edge")
    if not exe:
        return False, "没有找到 Chrome 或 Edge"

    if _cdp_alive():
        if url:
            _cdp_open_tab(url)
        return True, None

    os.makedirs(BROWSER_PROFILE, exist_ok=True)
    args = [exe, f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={BROWSER_PROFILE}",
            "--no-first-run", "--no-default-browser-check"]
    args.append(url or "about:blank")
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return False, f"启动失败：{e}"

    for _ in range(60):
        time.sleep(0.4)
        if _cdp_alive():
            return True, None
    return False, "浏览器启动超时"


def _cdp_open_tab(url):
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{CDP_PORT}/json/new?{urllib.parse.quote(url, safe='')}",
            method="PUT")
        urllib.request.urlopen(req, timeout=5).read()
        return True
    except Exception:
        return False


def _platform_domains(key):
    for k, _n, _u, doms in PLATFORMS:
        if k == key:
            return doms
    return ()


def read_browser_cookies():
    if not _cdp_alive():
        return []
    res = _cdp_get_cookies(CDP_PORT)
    return (res or {}).get("result", {}).get("cookies") or []


def connected_platforms(cookies=None):
    """只有出现真正的登录凭证，才算连上（页面自动设的匿名 cookie 不算）。"""
    if cookies is None:
        cookies = read_browser_cookies()
    names = {(c.get("name") or "", (c.get("domain") or "").lower()) for c in cookies}
    out = []
    for key, name, _url, doms in PLATFORMS:
        want = LOGIN_COOKIE.get(key)
        if not want:
            continue
        hit = any(n == want and any(d in h for d in doms) for n, h in names)
        if hit:
            out.append({"key": key, "name": name})
    return out, len(cookies)


# 各平台的登录凭证 cookie 名；没有列出来的平台只能手工判断
LOGIN_COOKIE = {
    "bilibili": "SESSDATA",
    "youtube": "SAPISID",
    "x": "auth_token",
    "douyin": "sessionid",
    "xiaohongshu": "web_session",
    "weibo": "SUB",
    "netease": "MUSIC_U",
    "qqmusic": "qm_keyst",
    "tiktok": "sessionid",
    "instagram": "sessionid",
    "vimeo": "vimeo",
    "kuaishou": "kuaishou.server.web_st",
}


def save_platform_cookies(key):
    """把某个平台的 cookie 合并进 cookies.txt。"""
    cookies = read_browser_cookies()
    doms = _platform_domains(key)
    if not doms:
        return 0
    subset = [c for c in cookies
              if any(d in (c.get("domain") or "").lower() for d in doms)]
    if not subset:
        return 0
    text = _cookies_to_netscape(subset)
    old = ""
    if os.path.isfile(COOKIE_FILE):
        with open(COOKIE_FILE, encoding="utf-8", errors="replace") as f:
            old = f.read()
    with open(COOKIE_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(_merge_cookie_text(old, text))
    return len(subset)


# ---------- 从浏览器自动取 cookie（走 CDP，绕开 v20 加密） ----------
BROWSER_EXES = {
    "Chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
               r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
    "Edge": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
             r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
}


def _find_browser_exe(key):
    for p in BROWSER_EXES.get(key, []):
        if os.path.isfile(p):
            return p
    return None


def _is_running(exe):
    name = os.path.basename(exe).lower()
    try:
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}"],
                           capture_output=True, text=True, timeout=10,
                           encoding="utf-8", errors="replace")
        return name in (r.stdout or "").lower()
    except Exception:
        return True


def _cdp_port_ready(port):
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=1.5):
            return True
    except Exception:
        return False


def _cdp_get_cookies(port, timeout=20):
    """通过 CDP 向浏览器要明文 cookie。"""
    import asyncio

    try:
        import websockets
    except Exception:
        return None

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version",
                                    timeout=5) as r:
            ws_url = json.load(r).get("webSocketDebuggerUrl")
    except Exception:
        return None
    if not ws_url:
        return None

    async def run():
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies",
                                      "params": {}}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                if msg.get("id") == 1:
                    return msg

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(run())
    except Exception:
        return None
    finally:
        try:
            loop.close()
        except Exception:
            pass


def _cookies_to_netscape(cookies):
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        domain = _clean_field(c.get("domain")).strip()
        name = _clean_field(c.get("name")).strip()
        if not domain or not name:
            continue
        expires = int(c.get("expires") or 0)
        if expires <= 0:
            expires = 9999999999
        lines.append("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            _clean_field(c.get("path")) or "/",
            "TRUE" if c.get("secure") else "FALSE",
            str(expires), name, _clean_field(c.get("value")),
        ]))
    return "\n".join(lines) + "\n"


def browser_status():
    """当前各浏览器的状态，给界面先展示再决定操作。"""
    out = []
    for key in ("Chrome", "Edge"):
        exe = _find_browser_exe(key)
        if not exe:
            out.append({"key": key, "installed": False, "running": False,
                        "cdp": False, "path": ""})
            continue
        port = 9333 if key == "Chrome" else 9334
        out.append({"key": key, "installed": True,
                    "running": _is_running(exe),
                    "cdp": _cdp_port_ready(port),
                    "path": exe})
    return out


def auto_grab_cookies(reopen_url=None, use_headless=False, only=None):
    """用浏览器自己的默认 profile 读 cookie。

    关键点：
      1. Chrome 127+ 的 cookie 用 App-Bound 加密锁死，离线解不开，
         只能让浏览器自己开调试端口读，所以浏览器要以调试模式启动一次。
      2. 启动后必须等页面真正加载完再读，否则 cookie 还没从磁盘载入，
         读到的是空列表。
      3. 读完**不关闭浏览器**，留着继续用；下次再点就能直接读，不用重启。
    """
    notes = []
    order = (only,) if only else ("Chrome", "Edge")
    for key in order:
        exe = _find_browser_exe(key)
        if not exe:
            notes.append(f"{key} 未安装")
            continue

        port = 9333 if key == "Chrome" else 9334
        running = _is_running(exe)
        cdp = _cdp_port_ready(port)

        try:
            if running and cdp:
                # 已经是调试模式，直接读
                notes.append(f"{key} 已在调试模式")
            else:
                if running:
                    # 普通模式启动的，只能重启一次才能开调试端口
                    subprocess.run(["taskkill", "/IM", os.path.basename(exe), "/F"],
                                   capture_output=True, timeout=20)
                    for _ in range(24):
                        time.sleep(0.5)
                        if not _is_running(exe):
                            break
                    else:
                        notes.append(f"{key} 关不掉")
                        continue
                args = [exe, f"--remote-debugging-port={port}",
                        "--no-first-run", "--no-default-browser-check",
                        "--restore-last-session"]
                subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                ok = False
                for _ in range(60):
                    time.sleep(0.4)
                    if _cdp_port_ready(port):
                        ok = True
                        break
                if not ok:
                    notes.append(f"{key} 调试端口没起来")
                    continue
                # 等页面把 cookie 从磁盘载进来，太急会读到空
                time.sleep(6)

            res = None
            for attempt in range(4):
                res = _cdp_get_cookies(port)
                cookies = (res or {}).get("result", {}).get("cookies") or []
                if cookies:
                    return _cookies_to_netscape(cookies), key, notes
                time.sleep(2.5)

            notes.append(f"{key} 里没有 cookie（可能没登录过任何账号）")

        except Exception as e:
            notes.append(f"{key} 出错：{str(e)[:60]}")
        # 注意：无论成功失败都不 taskkill，浏览器留着继续用
    return None, None, notes


def _is_internal_host(host):
    """内网 / 回环 / 保留地址：不让服务去请求它们（防 SSRF）。"""
    h = (host or "").lower().strip("[]")
    if not h:
        return True
    if h in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "metadata.google.internal"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _url_allowed(url):
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    return not _is_internal_host(p.hostname)


def unique_base(out, name):
    """同名文件不覆盖，自动加 (2)(3)。"""
    exts = ("mp4", "mkv", "webm", "mp3", "m4a", "mov", "flv")

    def taken(n):
        return any(os.path.exists(os.path.join(out, f"{n}.{e}")) for e in exts)

    if not taken(name):
        return name
    i = 2
    while taken(f"{name} ({i})"):
        i += 1
    return f"{name} ({i})"


def _merge_cookie_text(old, new):
    """把新导出的 cookie 合并进已有文件，同 (域名,路径,名) 覆盖，其余保留。
    这样可以一个平台一个平台地导，不用自己拼文件。"""
    def parse(text):
        rows = {}
        for line in (text or "").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split("\t")
            if len(parts) >= 7:
                rows[(parts[0], parts[2], parts[5])] = s
        return rows

    merged = parse(old)
    merged.update(parse(new))
    lines = ["# Netscape HTTP Cookie File"]
    lines += [merged[k] for k in merged]
    return "\n".join(lines) + "\n"


def _need_cookie(url):
    host = (urllib.parse.urlparse(url).netloc or "").lower()
    return any(k in host for k in NEED_COOKIE_HOSTS)


def _cookie_variants(url):
    """按优先级给出几组 cookie 配置：本地 cookies.txt → 浏览器 → 不带。"""
    if not _need_cookie(url):
        return [{}]
    out = []
    if os.path.isfile(COOKIE_FILE):
        out.append({"cookiefile": COOKIE_FILE})
    for br in ("chrome", "edge", "firefox"):
        out.append({"cookiesfrombrowser": (br,)})
    out.append({})
    return out


def _ytdlp_available():
    try:
        import yt_dlp  # noqa: F401
        return True
    except Exception:
        return False


# 这些域名直连更快也更稳，绕代理反而容易被判异常
DOMESTIC_HOSTS = ("163.com", "bilibili", "douyin", "kuaishou", "xiaohongshu",
                  "kuwo", "qq.com", "kugou", "iqiyi", "youku", "weibo",
                  "zhihu", "acgvideo", "hdslb", "b23.tv")


def ytdlp_probe(url, timeout=45):
    """用 yt-dlp 取元数据（不下文件）。超时返回 None，交给下一条通道。"""
    if not _ytdlp_available():
        return None
    box = {}

    def run():
        import yt_dlp
        base = {"quiet": True, "no_warnings": True, "skip_download": True,
                "noplaylist": True, "socket_timeout": 15, "nocheckcertificate": True}
        for extra in _cookie_variants(url):
            opts = dict(base)
            opts.update(extra)
            try:
                with yt_dlp.YoutubeDL(opts) as y:
                    info = y.extract_info(url, download=False)
                if info:
                    box["info"] = info
                    return
            except Exception:
                continue
        box["info"] = None

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None

    info = box.get("info")
    if not info:
        return None
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return None
        info = entries[0]
    return info


def ytdlp_options(info):
    """把 yt-dlp 的 formats 压成几档画质选项。纯音频站直接给一条音频。"""
    formats = info.get("formats") or []
    heights = set()
    has_audio = False
    for f in formats:
        if f.get("acodec") not in (None, "none"):
            has_audio = True
        if f.get("vcodec") in (None, "none"):
            continue
        h = f.get("height")
        if h:
            heights.add(int(h))

    # 没有任何视频轨 = 纯音频站（网易云 / SoundCloud 等）
    if not heights:
        abr = info.get("abr") or 0
        try:
            abr = int(float(abr))
        except (TypeError, ValueError):
            abr = 0
        label = f"{abr}K" if abr else "音频"
        return [], [{"label": label, "qn": 0, "via": "ytdlp", "url": ""}]

    hs = sorted(heights, reverse=True)[:4]
    videos = [{"label": f"{h}P", "height": h, "qn": 0, "via": "ytdlp", "url": ""}
              for h in hs]
    audios = [{"label": "音频", "qn": 0, "via": "ytdlp", "url": ""}] if has_audio else []
    return videos, audios


def run_ytdlp_job(tid, url, name, kind, height):
    out = load_out()
    parts = os.path.join(out, ".parts")
    try:
        import yt_dlp
        os.makedirs(out, exist_ok=True)
        name = unique_base(out, name)
        ff = find_ffmpeg()

        def hook(d):
            st = d.get("status")
            if st == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                pct = int(done * 90 / total) if total else 0
                set_task(tid, state="downloading", progress=max(0, min(pct, 90)),
                         stage="下载中")
            elif st == "finished":
                set_task(tid, progress=92, stage="合并中")

        parts = os.path.join(out, ".parts")
        os.makedirs(parts, exist_ok=True)

        opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "noprogress": True,
            # 分片和 .part 全部落在 .parts 里，下完才移动到目标目录，
            # 避免用户在下载目录里看到 xxx.f137.mp4.part 这种半成品
            # 换成 unique_base 时也把 % 转义，否则 yt-dlp 会当模板变量解析
            "outtmpl": name.replace("%", "%%") + ".%(ext)s",
            "paths": {"home": out, "temp": parts},
            "overwrites": True,
            "progress_hooks": [hook],
            "retries": 5,
            "fragment_retries": 5,
            "nocheckcertificate": True,
        }
        if ff:
            opts["ffmpeg_location"] = os.path.dirname(ff)

        # 国内站点直连：走系统代理常被 CDN 判成异常流量（403）
        host = (urllib.parse.urlparse(url).netloc or "").lower()
        if any(k in host for k in DOMESTIC_HOSTS):
            opts["proxy"] = ""

        if kind == "mp3":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3", "preferredquality": "192"}]
            want = ("mp3",)
        else:
            h = f"[height<={height}]" if height else ""
            # 优先 H.264 + AAC：AV1/VP9 在 Windows 自带播放器里经常打不开
            opts["format"] = (
                f"bv*[vcodec^=avc1]{h}+ba[acodec^=mp4a]/"
                f"bv*[vcodec^=avc]{h}+ba/"
                f"bv*{h}+ba/"
                f"b{h}/best")
            opts["merge_output_format"] = "mp4"
            want = ("mp4", "mkv", "webm")

        set_task(tid, state="downloading", stage="准备中", progress=0)

        # 需要登录的站点轮换 cookie 来源；其余站点只有一个空配置
        variants = _cookie_variants(url)
        err = None
        tries = max(3, len(variants))
        for attempt in range(tries):
            o = dict(opts)
            o.update(variants[attempt % len(variants)])
            try:
                with yt_dlp.YoutubeDL(o) as y:
                    y.download([url])
                err = None
                break
            except Exception as e:
                err = e
                msg = str(e)
                if attempt < tries - 1 and ("403" in msg or "Forbidden" in msg
                                            or "timed out" in msg or "429" in msg
                                            or "login" in msg.lower()
                                            or "cookie" in msg.lower()):
                    set_task(tid, stage=f"重试 {attempt + 2}/{tries}", progress=3)
                    time.sleep(3 + attempt * 3)
                    continue
                break
        if err:
            raise err

        for ext in want:
            p = os.path.join(out, f"{name}.{ext}")
            if os.path.exists(p):
                set_task(tid, state="done", progress=100, stage="完成",
                         file=os.path.basename(p))
                return
        raise RuntimeError("下载结束但没找到输出文件")
    except Exception as e:
        set_task(tid, state="error", stage=str(e)[:200])
    finally:
        # 失败或中断时清掉半成品，别留在磁盘上
        try:
            if os.path.isdir(parts):
                for fn in os.listdir(parts):
                    if fn.startswith(name + "."):
                        os.remove(os.path.join(parts, fn))
        except Exception:
            pass
        _clean_parts(parts)


# ============================================================ 取标题
_TITLE_CACHE = {}


def _http_text(url, referer=None, timeout=8, opener=None):
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    op = opener or urllib.request.build_opener()
    return op.open(req, timeout=timeout).read().decode("utf-8", "replace")


def bili_title(bvid):
    """B 站标题：必须先访问首页拿 buvid3 cookie，否则接口返回风控页。"""
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    try:
        _http_text("https://www.bilibili.com/", timeout=8, opener=op)
    except Exception:
        pass
    raw = _http_text(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        referer="https://www.bilibili.com/", timeout=8, opener=op)
    d = json.loads(raw)
    if d.get("code") != 0:
        return None
    v = d.get("data") or {}
    return (v.get("title") or "").strip() or None


def page_title(url):
    """通用兜底：读网页 <title>，去掉站点后缀。"""
    try:
        html = _http_text(url, timeout=8)
    except Exception:
        return None
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if not m:
        return None
    t = re.sub(r"\s+", " ", m.group(1)).strip()
    for suf in ("_哔哩哔哩_bilibili", "-抖音", "- 抖音", " - 小红书", "_小红书"):
        t = t.replace(suf, "")
    t = t.strip(" -_|·")
    return t if len(t) >= 3 and not re.fullmatch(r"(抖音|小红书|哔哩哔哩|bilibili)", t) else None


def fetch_title(url):
    """按链接猜出视频标题；拿不到返回 None。"""
    key = url
    if key in _TITLE_CACHE:
        return _TITLE_CACHE[key]
    title = None
    try:
        m = re.search(r"(BV[0-9A-Za-z]{10})", url)
        if m:
            title = bili_title(m.group(1))
        if not title:
            title = page_title(url)
    except Exception:
        title = None
    _TITLE_CACHE[key] = title
    return title


# ---- 保存目录配置 ----
def load_out():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            p = (json.load(f) or {}).get("out")
        if p and os.path.isdir(p):
            return p
    except Exception:
        pass
    return DEFAULT_OUT


def save_out(path):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"out": path}, f, ensure_ascii=False, indent=2)


def common_places():
    home = os.path.expanduser("~")
    cands = [
        ("桌面", os.path.join(home, "Desktop")),
        ("下载", os.path.join(home, "Downloads")),
        ("视频", os.path.join(home, "Videos")),
        ("C 盘", "C:\\"),
        ("D 盘", "D:\\"),
        ("E 盘", "E:\\"),
    ]
    return [{"name": n, "path": p} for n, p in cands if os.path.isdir(p)]


def pick_dir_dialog():
    """弹 Windows 原生文件夹选择框。不依赖 tkinter（精简版 Python 常缺）。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        BIF_RETURNONLYFSDIRS = 0x0001
        BIF_EDITBOX = 0x0010
        BIF_NEWDIALOGSTYLE = 0x0040

        class BROWSEINFO(ctypes.Structure):
            _fields_ = [
                ("hwndOwner", wintypes.HWND),
                ("pidlRoot", ctypes.c_void_p),
                ("pszDisplayName", ctypes.c_void_p),
                ("lpszTitle", wintypes.LPCWSTR),
                ("ulFlags", wintypes.UINT),
                ("lpfn", ctypes.c_void_p),
                ("lParam", wintypes.LPARAM),
                ("iImage", ctypes.c_int),
            ]

        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
        shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFO)]
        shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
        shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]

        ole32.CoInitialize(None)
        display = ctypes.create_unicode_buffer(260)
        bi = BROWSEINFO()
        bi.hwndOwner = ctypes.windll.kernel32.GetConsoleWindow() or None
        bi.pszDisplayName = ctypes.addressof(display)
        bi.lpszTitle = "选择视频保存位置"
        bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_EDITBOX | BIF_NEWDIALOGSTYLE

        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return None
        path = ctypes.create_unicode_buffer(260)
        ok = shell32.SHGetPathFromIDListW(ctypes.c_void_p(pidl), path)
        ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
        return path.value if ok else None
    except Exception:
        return None


# ---- 解析端（逆向得到的签名算法，仅用于取回媒体地址）----
API_BASE = "https://www.xtdowner.com"
SALT_INNER = "XAZDZD"
SALT_OUTER = "XTv2"

TASKS = {}
LOCK = threading.Lock()
AUTO_LOCK = threading.Lock()
def _memory_gb():
    """返回 (物理内存总量, 可用内存)，单位 GB。"""
    if sys.platform != "win32":
        try:
            with open("/proc/meminfo") as f:
                kv = {}
                for line in f:
                    k, _, v = line.partition(":")
                    kv[k.strip()] = int(v.split()[0]) / (1024 ** 2)
                return kv.get("MemTotal", 8.0), kv.get("MemAvailable", 4.0)
        except Exception:
            return 8.0, 4.0
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return (st.ullTotalPhys / (1024 ** 3), st.ullAvailPhys / (1024 ** 3))
    except Exception:
        return 8.0, 4.0


RAM_TOTAL_GB, RAM_AVAIL_GB = _memory_gb()


def _upload_limit_bytes():
    """上传上限只看内存，不受某个盘剩多少影响。

    - 内存的 1/2，下限 2 GB，上限 32 GB
    - 文件本身是流式写到「上传目录」（跟随下载目录，可设到大容量盘），
      不驻留内存，所以内存不是瓶颈、磁盘才是；但上限不该被系统盘剩余空间压死。
    """
    by_ram = max(2.0, min(RAM_TOTAL_GB * 0.5, 32.0))
    return int(by_ram * 1024 ** 3), by_ram


UPLOAD_MAX, UPLOAD_MAX_GB = _upload_limit_bytes()


# ============================================================ 签名 / 解析
def make_sign(s, now_ms=None):
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    tag = f"{now_ms & 0xFF:02x}"
    inner = hashlib.md5((s + tag + "C" + SALT_INNER).encode()).hexdigest()
    outer = hashlib.sha256((SALT_OUTER + inner + SALT_INNER).encode()).hexdigest()
    return "xtdo_" + tag + outer[16:]


def enc(s):
    return urllib.parse.quote(s, safe="!'()*-._~")


class Resolver:
    def __init__(self):
        import http.cookiejar
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))

    def _headers(self):
        return {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}

    def _csrf(self):
        req = urllib.request.Request(f"{API_BASE}/video/", headers=self._headers())
        page = self.opener.open(req, timeout=60).read().decode("utf-8", "replace")
        m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)
        if not m:
            raise RuntimeError("初始化失败")
        return m.group(1)

    def _post(self, data):
        req = urllib.request.Request(
            f"{API_BASE}/video/", data=urllib.parse.urlencode(data).encode(),
            headers={**self._headers(), "Referer": f"{API_BASE}/video/",
                     "Origin": API_BASE,
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            return self.opener.open(req, timeout=90).read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.read().decode("utf-8", "replace")

    def resolve(self, url):
        tok = self._csrf()
        page = self._post({"csrfmiddlewaretoken": tok, "detail": url,
                           "platform": "video",
                           "sign": make_sign(enc("xtdowner_" + url))})
        m = re.search(r'name="dyid" value="([^"]+)"', page)
        if m:
            import html as h
            dyid = h.unescape(m.group(1))
            t2 = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)
            page = self._post({"csrfmiddlewaretoken": t2.group(1) if t2 else tok,
                               "dyid": dyid,
                               "sign": make_sign(enc("xtdowner_" + dyid))})

        import html as h
        clean = h.unescape(page)
        links, seen = [], set()
        for mm in re.finditer(
                r'https?://[^"\'\s<>]+?\.(?:mp4|m4s|mp3|m4a)(?:\?[^"\'\s<>]*)?', clean):
            u = mm.group(0).replace("&amp;", "&")
            if u not in seen:
                seen.add(u)
                links.append(u)
        return links


# B 站画质代号 → 人话
QN_LABEL = {30011: "360P", 30032: "480P", 30064: "720P", 30080: "1080P",
            30112: "1080P+", 30116: "1080P60", 30120: "4K", 30121: "8K",
            30216: "64K", 30232: "132K", 30280: "192K", 30300: "杜比"}


# 从任意文本里捞出链接：兼容“整段分享文案”“无协议头域名”“全角标点包裹”
_URL_RE = re.compile(r'https?://[^\s<>"\'，。；、！？）】》」』]+', re.I)
_BARE_RE = re.compile(
    r'(?:[a-z0-9][a-z0-9-]*\.)+[a-z]{2,}(?::\d+)?'
    r'(?:/[^\s<>"\'，。；、！？）】》」』]*)?', re.I)


def extract_url(raw):
    """返回 (归一化链接 or None, 是否做过提取)。"""
    s = (raw or "").replace("\u200b", "").replace("\ufeff", "").strip()
    m = _URL_RE.search(s)
    if m:
        u = m.group(0).rstrip(".,;:!?")
        return u, u != s
    m = _BARE_RE.search(s)
    if m:
        u = "https://" + m.group(0).rstrip(".,;:!?")
        return u, True
    return None, False


SHORT_HOSTS = ("b23.tv", "v.douyin.com", "xhslink.com", "t.cn", "dwz.cn",
               "v.kuaishou.com", "url.cn", "suo.im")


def expand_short(url, timeout=8):
    """短链先跟一次跳转拿真实地址；失败就原样返回，不影响主流程。"""
    host = (urllib.parse.urlparse(url).netloc or "").lower()
    if not any(h in host for h in SHORT_HOSTS):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            final = r.geturl()
        return final if final and final != url else None
    except Exception:
        return None


def classify(links):
    videos, audios, mp4s = [], [], []
    for u in links:
        path = u.split("?")[0]
        if path.endswith(".mp4"):
            mp4s.append({"url": u, "label": "单文件", "qn": 0})
            continue
        m = re.search(r"-(\d+)\.m4s$", path)
        qn = int(m.group(1)) if m else 0
        item = {"url": u, "qn": qn, "label": QN_LABEL.get(qn, str(qn) if qn else "流")}
        if qn >= 30200:
            audios.append(item)
        elif path.endswith((".mp3", ".m4a")):
            audios.append(item)
        else:
            videos.append(item)
    videos.sort(key=lambda x: -x["qn"])
    audios.sort(key=lambda x: -x["qn"])
    return videos, audios, mp4s


# ============================================================ 下载
def cdn_headers(url):
    host = (urllib.parse.urlparse(url).netloc or "").lower()
    if "bilivideo" in host or "bilibili" in host:
        ref = "https://www.bilibili.com/"
    elif "douyin" in host or "bytecdn" in host or "zjcdn" in host:
        ref = "https://www.douyin.com/"
    elif "xhscdn" in host or "xiaohongshu" in host:
        ref = "https://www.xiaohongshu.com/"
    else:
        ref = f"https://{host}/"
    return {"User-Agent": UA, "Referer": ref, "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9", "Origin": ref.rstrip("/")}


def find_ffmpeg():
    # 1) exe 旁边 bin\ffmpeg.exe（用户可自行替换版本）
    local = os.path.join(HERE, "bin", "ffmpeg.exe")
    if os.path.isfile(local):
        return local
    # 2) 打包进 exe 的（PyInstaller 解到临时目录）
    bundled = os.path.join(RES_DIR, "ffmpeg.exe")
    if os.path.isfile(bundled):
        return bundled
    # 3) 系统里现有的
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    for p in (r"E:\ffmpeg\bin\ffmpeg.exe", r"C:\ffmpeg\bin\ffmpeg.exe"):
        if os.path.isfile(p):
            return p
    return None


def save_stream(url, dest, on_progress=None):
    """下载到 dest。文件已存在且有内容时用 Range 续传，断网不用从头来。"""
    have = os.path.getsize(dest) if os.path.isfile(dest) else 0
    req = urllib.request.Request(url, headers=cdn_headers(url))
    if have:
        req.add_header("Range", f"bytes={have}-")
    try:
        r = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        if e.code == 416:          # 已经有全量了
            return
        raise

    with r:
        resumed = have and r.getcode() == 206
        mode = "ab" if resumed else "wb"
        base = have if resumed else 0
        total = base + int(r.headers.get("Content-Length") or 0)

        # 空间不够就别开写，免得下到一半失败还留个半截文件
        need = total - base
        if need > 0:
            try:
                free = shutil.disk_usage(os.path.dirname(dest) or ".").free
                if need > free - 200 * 1024 * 1024:
                    raise RuntimeError(
                        f"磁盘空间不足：这个文件还需要 {need / 1024 ** 3:.2f} GB，"
                        f"但目标盘只剩 {free / 1024 ** 3:.2f} GB")
            except RuntimeError:
                raise
            except Exception:
                pass

        got = base
        with open(dest, mode) as f:
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if on_progress:
                    on_progress(got, total)


TASK_SEM = threading.Semaphore(2)   # 同时最多跑 2 个 ffmpeg 任务，多了打满 CPU
DL_SEM = threading.Semaphore(3)     # 下载是 IO 密集型，可以比转码多几个

PROCS = {}          # tid -> Popen，用于中途取消
CANCELLED = set()   # 被用户取消的 tid


def _kill_task(tid):
    """杀掉正在跑的任务进程。返回是否真的杀掉了。"""
    p = PROCS.get(tid)
    if not p or p.poll() is not None:
        return False
    CANCELLED.add(tid)
    try:
        p.kill()
        return True
    except Exception:
        return False


def _clean_uploads(max_age_h=24):
    """只清理上传中断留下的残片，**不动用户上传的源文件**。

    源文件可能是用户唯一的副本，程序不替用户做删除决定。
    要腾空间自己删 upload_dir() 里的东西就行。
    """
    try:
        d = upload_dir()
        now = time.time()
        removed = 0
        freed = 0
        for name in os.listdir(d):
            # 只处理 .incoming_ 这种半截文件，正常上传的文件一律保留
            if not name.startswith(".incoming"):
                continue
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            try:
                if now - os.path.getmtime(p) > 3600:
                    freed += os.path.getsize(p)
                    os.remove(p)
                    removed += 1
            except OSError:
                pass
        if removed:
            print(f"[clean] 清理中断残留 {removed} 个，释放 {freed / 1024 ** 2:.1f} MB")
    except Exception:
        pass


def _upload_janitor():
    """后台每小时清一次上传缓存。"""
    while True:
        time.sleep(3600)
        _clean_uploads()


def set_task(tid, **kw):
    with LOCK:
        TASKS.setdefault(tid, {}).update(kw)
        # 任务记录不清会一直涨，超量就把已结束的丢掉
        if len(TASKS) > 300:
            for k in [k for k, v in TASKS.items()
                      if v.get("state") in ("done", "error")]:
                TASKS.pop(k, None)
                if len(TASKS) <= 200:
                    break


def _clean_parts(parts):
    """临时目录空了就删掉，别在下载目录留一个空文件夹。"""
    try:
        if os.path.isdir(parts) and not os.listdir(parts):
            os.rmdir(parts)
    except Exception:
        pass


def upload_dir():
    """上传的临时文件跟着下载目录走——用户通常把下载放在大容量盘，
    放系统盘（%LOCALAPPDATA%）遇到 C 盘紧张会直接失败。"""
    try:
        base = os.path.dirname(load_out().rstrip("\\/")) or DATA_DIR
        if not os.path.isdir(base):
            base = DATA_DIR
    except Exception:
        base = DATA_DIR
    d = os.path.join(base, ".uploads")
    os.makedirs(d, exist_ok=True)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(d, 0x02)   # 隐藏
        except Exception:
            pass
    return d


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v", ".ts", ".wmv"}


def safe_upload_name(fn):
    base = os.path.basename(fn or "video")
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in VIDEO_EXTS:
        ext = ".mp4"
    stem = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", stem).strip(" .") or "video"
    return stem[:60] + ext


def _gif_cmd(ff, src, out, fps, width, start, duration):
    vf = (f"fps={fps},scale={width}:-1:flags=lanczos,"
          f"split[s0][s1];[s0]palettegen=stats_mode=diff[p];"
          f"[s1][p]paletteuse=dither=bayer:bayer_scale=3")
    cmd = [ff, "-y", "-loglevel", "error"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", src]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-vf", vf, "-loop", "0", out]
    return cmd


def _render_gif(tid, ff, src, out, fps, width, start, duration, pct_from, pct_to):
    """跑一次转换，带进度。"""
    cmd = _gif_cmd(ff, src, out, fps, width, start, duration)
    total = _video_duration(ff, src) or 0
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace",
                            universal_newlines=True)
    PROCS[tid] = proc
    # 简化：只在开始时给个进度，避免 -stats 与迭代冲突
    set_task(tid, progress=pct_from)
    proc.communicate()
    PROCS.pop(tid, None)
    if tid in CANCELLED:
        CANCELLED.discard(tid)
        raise RuntimeError("已取消")
    set_task(tid, progress=pct_to)
    return proc.returncode == 0 and os.path.isfile(out)


def to_gif_job(tid, src, out_dir, base, fps, width, start, duration,
               target_kb=None):
    """转 GIF。给了 target_kb 就自动算分辨率，压到目标大小以内。"""
    with TASK_SEM:
        _to_gif_job(tid, src, out_dir, base, fps, width, start, duration, target_kb)


def _to_gif_job(tid, src, out_dir, base, fps, width, start, duration,
                target_kb=None):
    try:
        ff = find_ffmpeg()
        if not ff:
            raise RuntimeError("没有 ffmpeg，无法转换")
        os.makedirs(out_dir, exist_ok=True)
        base = unique_base(out_dir, base)

        fps = max(2, min(30, int(fps or 12)))
        start = start or None
        duration = duration or None

        # ---- 目标体积模式：迭代调宽度 ----
        if target_kb:
            target_bytes = max(20 * 1024, int(float(target_kb) * 1024))
            total_dur = _video_duration(ff, src) or 0
            eff = float(duration) if duration else max(0.2, total_dur - float(start or 0))

            tmpdir = os.path.join(DATA_DIR, ".uploads")
            os.makedirs(tmpdir, exist_ok=True)
            trial = os.path.join(tmpdir, f"{tid}.trial.gif")
            width_now = 640
            best = None

            for attempt in range(6):
                set_task(tid, stage=f"压缩中 第{attempt + 1}次 · {width_now}px",
                         progress=10 + attempt * 14)
                try:
                    ok = _render_gif(tid, ff, src, trial, fps, width_now,
                                     start, duration, 10 + attempt * 14,
                                     10 + attempt * 14)
                except Exception:
                    ok = False
                if not ok:
                    break
                size = os.path.getsize(trial)
                best = (width_now, size)
                if size <= target_bytes:
                    break
                # GIF 体积约正比于像素面积，按面积比开方换算线性缩放
                ratio = (target_bytes / size) ** 0.6
                next_w = max(80, int(width_now * ratio))
                if next_w >= width_now:
                    next_w = max(80, width_now - 40)
                width_now = next_w
                if width_now <= 80 and size > target_bytes:
                    # 宽度到底还超，降帧率
                    fps = max(4, fps // 2)

            if not best:
                raise RuntimeError("转换失败，检查视频是否可读")

            final = os.path.join(out_dir, base + ".gif")
            # 临时文件在 .uploads，输出目录可能在另一个盘，os.replace 跨盘会失败
            shutil.move(trial, final)
            _verify_gif(ff, final, best[0], eff)
            set_task(tid, state="done", progress=100, stage="完成",
                     file=os.path.basename(final),
                     size=os.path.getsize(final),
                     note=f"目标 {target_kb}KB · {best[0]}px")
            return

        # ---- 手动模式：按指定宽度直接转 ----
        final = os.path.join(out_dir, base + ".gif")
        width = max(80, min(1280, int(width or 480)))
        set_task(tid, state="downloading", stage="转换中", progress=5)
        if not _render_gif(tid, ff, src, final, fps, width, start, duration, 5, 95):
            raise RuntimeError("转换失败，检查视频是否可读")
        want = float(duration) if duration else max(0.2, (_video_duration(ff, src) or 0) - float(start or 0))
        _verify_gif(ff, final, width, want)
        set_task(tid, state="done", progress=100, stage="完成",
                 file=os.path.basename(final), size=os.path.getsize(final))
    except Exception as e:
        set_task(tid, state="error", stage=str(e)[:200], progress=100)


def _verify_gif(ff, path, expect_width=None, expect_dur=None):
    """GIF 也需要核对，避免静默产出坏文件。"""
    w, h, _, d = _probe_video(ff, path)
    if d <= 0 or w <= 0 or h <= 0:
        raise RuntimeError("GIF 输出读不出来，转换失败")
    if os.path.getsize(path) < 1024:
        raise RuntimeError("GIF 文件过小，转换失败")
    if expect_width and abs(w - int(expect_width)) > 8:
        raise RuntimeError(f"GIF 宽度不对（要求 {int(expect_width)}px，实际 {w}px）")
    if expect_dur and expect_dur > 1.5 and abs(d - expect_dur) / expect_dur > 0.3:
        raise RuntimeError(
            f"GIF 时长异常（预期 {expect_dur:.1f}s，实际 {d:.1f}s），转换可能中断")


# 各输出高度下 H.264 的相对合理最低码率（kbps）。低于这个值画面开始糊、
# 大面积拖影，再低就成马赛克了——不如直接拒绝，让用户把体积调大。
MIN_KBPS_BY_HEIGHT = {360: 400, 480: 700, 720: 1200, 1080: 2000,
                      1440: 3500, 2160: 6000}


def _probe_video(ff, path, timeout=20):
    """读出 (宽, 高, 帧率, 时长)；失败返回全 0。"""
    try:
        r = subprocess.run([ff, "-i", path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        s = r.stderr or ""
        m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", s)
        fr = re.search(r"(\d+(?:\.\d+)?)\s*fps", s)
        dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", s)
        w = int(m.group(1)) if m else 0
        h = int(m.group(2)) if m else 0
        f = float(fr.group(1)) if fr else 0.0
        d = ((int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3)))
             if dur else 0.0)
        return w, h, f, d
    except Exception:
        return 0, 0, 0.0, 0.0


def _min_video_kbps(height, fps=30):
    """按目标分辨率和帧率算出可接受的最低视频码率。"""
    try:
        h = int(height or 720)
    except (TypeError, ValueError):
        h = 720
    pick = 8000
    for th in sorted(MIN_KBPS_BY_HEIGHT):
        if h <= th:
            pick = MIN_KBPS_BY_HEIGHT[th]
            break
    # 高帧率需要更多码率，但不必线性放大
    try:
        f = float(fps or 30)
        if f > 30:
            pick = int(pick * (f / 30) ** 0.7)
    except (TypeError, ValueError):
        pass
    return pick


def trim_job(tid, src, out_dir, base, start, duration, mode="reencode"):
    """裁剪片段。mode=copy 快但只能切在关键帧上，reencode 精确但慢。"""
    with TASK_SEM:
        _trim_job(tid, src, out_dir, base, start, duration, mode)


def _trim_job(tid, src, out_dir, base, start, duration, mode="reencode"):
    try:
        ff = find_ffmpeg()
        if not ff:
            raise RuntimeError("没有 ffmpeg，无法裁剪")
        os.makedirs(out_dir, exist_ok=True)
        total = _video_duration(ff, src) or 0
        st = max(0.0, float(start or 0))
        du = float(duration) if duration else None
        if total and st >= total:
            raise RuntimeError(f"开始时间 {st:.1f}s 超出了视频总长 {total:.1f}s")
        if du is None and total:
            du = max(0.5, total - st)

        final = os.path.join(out_dir, unique_base(out_dir, base) + ".mp4")
        cmd = [ff, "-y", "-loglevel", "error", "-stats"]
        # -ss 放在 -i 前面是快速定位，配合 copy 几乎瞬间完成
        if st > 0:
            cmd += ["-ss", f"{st:.3f}"]
        cmd += ["-i", src]
        if du:
            cmd += ["-t", f"{du:.3f}"]
        if mode == "copy":
            cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"]
        cmd += ["-movflags", "+faststart", final]

        set_task(tid, state="downloading", stage="裁剪中", progress=5)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace")
        PROCS[tid] = proc
        pct = 5
        for line in proc.stderr:
            m = re.search(r"time=(\d+):(\d+):(\d+\.?\d*)", line)
            if m and du:
                sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                pct = max(pct, min(96, 5 + int(sec / du * 91)))
                set_task(tid, progress=pct)
        proc.wait()
        PROCS.pop(tid, None)
        if tid in CANCELLED:
            CANCELLED.discard(tid)
            raise RuntimeError("已取消")

        if proc.returncode != 0 or not os.path.isfile(final):
            raise RuntimeError("裁剪失败，检查时间段是否超出视频范围")

        _, _, _, od = _probe_video(ff, final)
        if od <= 0:
            raise RuntimeError("输出文件读不出来，裁剪失败")

        set_task(tid, state="done", progress=100, stage="完成",
                 file=os.path.basename(final), size=os.path.getsize(final),
                 note=f"{st:.1f}s 起 · 时长 {od:.1f}s")
    except Exception as e:
        set_task(tid, state="error", stage=str(e)[:200], progress=100)


AUDIO_CODEC = {
    "mp3": (["-c:a", "libmp3lame", "-q:a", "2"], ".mp3"),
    "m4a": (["-c:a", "aac", "-b:a", "192k"], ".m4a"),
    "wav": (["-c:a", "pcm_s16le"], ".wav"),
}


def audio_job(tid, src, out_dir, base, fmt="mp3", start=None, duration=None):
    """从本地视频抽音轨。"""
    with TASK_SEM:
        _audio_job(tid, src, out_dir, base, fmt, start, duration)


def _audio_job(tid, src, out_dir, base, fmt="mp3", start=None, duration=None):
    try:
        ff = find_ffmpeg()
        if not ff:
            raise RuntimeError("没有 ffmpeg，无法提取音频")
        os.makedirs(out_dir, exist_ok=True)
        fmt = fmt if fmt in AUDIO_CODEC else "mp3"
        codec, ext = AUDIO_CODEC[fmt]
        total = _video_duration(ff, src) or 0

        final = os.path.join(out_dir, unique_base(out_dir, base) + ext)
        cmd = [ff, "-y", "-loglevel", "error", "-stats"]
        if start:
            cmd += ["-ss", f"{float(start):.3f}"]
        cmd += ["-i", src]
        if duration:
            cmd += ["-t", f"{float(duration):.3f}"]
        cmd += ["-vn"] + codec + [final]

        set_task(tid, state="downloading", stage="提取音频", progress=5)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace")
        PROCS[tid] = proc
        pct = 5
        for line in proc.stderr:
            m = re.search(r"time=(\d+):(\d+):(\d+\.?\d*)", line)
            if m and total:
                sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                pct = max(pct, min(96, 5 + int(sec / total * 91)))
                set_task(tid, progress=pct)
        proc.wait()
        PROCS.pop(tid, None)
        if tid in CANCELLED:
            CANCELLED.discard(tid)
            raise RuntimeError("已取消")

        if proc.returncode != 0 or not os.path.isfile(final):
            raise RuntimeError("提取失败，这个文件可能没有音轨")
        if os.path.getsize(final) < 1024:
            raise RuntimeError("音轨是空的，这个视频可能没有声音")

        set_task(tid, state="done", progress=100, stage="完成",
                 file=os.path.basename(final), size=os.path.getsize(final),
                 note=f"{fmt.upper()}")
    except Exception as e:
        set_task(tid, state="error", stage=str(e)[:200], progress=100)


CONVERT_PRESET = {
    "mp4": (["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"], ".mp4"),
    "webm": (["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
              "-c:a", "libopus", "-b:a", "96k"], ".webm"),
    "mov": (["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k"], ".mov"),
    "mkv": (["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k"], ".mkv"),
}


def convert_job(tid, src, out_dir, base, target="mp4"):
    """转容器格式。"""
    with TASK_SEM:
        _convert_job(tid, src, out_dir, base, target)


def _convert_job(tid, src, out_dir, base, target="mp4"):
    try:
        ff = find_ffmpeg()
        if not ff:
            raise RuntimeError("没有 ffmpeg，无法转格式")
        os.makedirs(out_dir, exist_ok=True)
        target = target if target in CONVERT_PRESET else "mp4"
        codec, ext = CONVERT_PRESET[target]
        total = _video_duration(ff, src) or 0

        final = os.path.join(out_dir, unique_base(out_dir, base) + ext)
        cmd = [ff, "-y", "-loglevel", "error", "-stats", "-i", src] + codec + [final]

        set_task(tid, state="downloading", stage=f"转 {target.upper()}", progress=5)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace")
        PROCS[tid] = proc
        pct = 5
        for line in proc.stderr:
            m = re.search(r"time=(\d+):(\d+):(\d+\.?\d*)", line)
            if m and total:
                sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                pct = max(pct, min(96, 5 + int(sec / total * 91)))
                set_task(tid, progress=pct)
        proc.wait()
        PROCS.pop(tid, None)
        if tid in CANCELLED:
            CANCELLED.discard(tid)
            raise RuntimeError("已取消")

        if proc.returncode != 0 or not os.path.isfile(final):
            raise RuntimeError("转换失败，检查源文件是否完好")
        if os.path.getsize(final) < 1024:
            raise RuntimeError("输出为空，转换失败")

        set_task(tid, state="done", progress=100, stage="完成",
                 file=os.path.basename(final), size=os.path.getsize(final),
                 note=f"→ {target.upper()}")
    except Exception as e:
        set_task(tid, state="error", stage=str(e)[:200], progress=100)


def compress_video_job(tid, src, out_dir, base, height, fps, crf, target_mb):
    """压缩视频。给了 target_mb 就按目标体积反算码率，否则按 CRF 走质量。

    target_mb 是"目标体积"，单位 MB（前端会把 KB/GB 换算好再传）。
    """
    with TASK_SEM:
        _compress_video_job(tid, src, out_dir, base, height, fps, crf, target_mb)


def _compress_video_job(tid, src, out_dir, base, height, fps, crf, target_mb):
    try:
        ff = find_ffmpeg()
        if not ff:
            raise RuntimeError("没有 ffmpeg，无法压缩")
        os.makedirs(out_dir, exist_ok=True)
        base = unique_base(out_dir, base)
        final = os.path.join(out_dir, base + ".mp4")

        total = _video_duration(ff, src) or 0
        vf = []
        if height:
            try:
                h = int(height)
                if 120 <= h <= 2160:
                    vf.append(f"scale=-2:{h}")
            except (TypeError, ValueError):
                pass
        if fps:
            try:
                f = float(fps)
                if 5 <= f <= 60:
                    vf.append(f"fps={f:g}")
            except (TypeError, ValueError):
                pass

        cmd = [ff, "-y", "-loglevel", "error", "-stats", "-i", src]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        cmd += ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]

        note = ""
        if target_mb:
            target = max(0.05, float(target_mb))
            if total <= 0:
                raise RuntimeError("读不出视频时长，没法按大小压缩")
            total_kbps = int(target * 1024 * 8 / total)
            audio_kbps = 128 if total_kbps > 700 else (96 if total_kbps > 350 else 64)
            video_kbps = max(60, total_kbps - audio_kbps)

            # 低于该分辨率的合理下限就不干了，避免压出糊成一团的片子
            need = _min_video_kbps(height, fps)
            if video_kbps < need:
                min_mb = (need + audio_kbps) * total / 8 / 1024
                h_txt = f"{height}p" if height else "原画"
                f_txt = f"{int(float(fps))}fps" if fps else "原帧率"
                raise RuntimeError(
                    f"目标太小，压不出能看的视频：{target:g}MB 分给 {total:.0f} 秒，"
                    f"视频只能拿到 {video_kbps}kbps，低于 {h_txt}/{f_txt} 的最低要求 {need}kbps。"
                    f"想保持这个清晰度，体积至少要 {min_mb:.0f}MB；"
                    f"或者把分辨率调到 480p 以下再试")

            cmd += ["-b:v", f"{video_kbps}k",
                    "-maxrate", f"{int(video_kbps * 1.25)}k",
                    "-bufsize", f"{video_kbps * 2}k"]
            cmd += ["-c:a", "aac", "-b:a", f"{audio_kbps}k"]
            if target >= 1:
                note = f"目标 {target:g}MB · 视频 {video_kbps}kbps"
            else:
                note = f"目标 {target * 1024:.0f}KB · 视频 {video_kbps}kbps"
        else:
            crf = max(16, min(32, int(crf or 23)))
            cmd += ["-crf", str(crf), "-c:a", "aac", "-b:a", "128k"]
            note = f"质量 CRF {crf}"

        cmd += ["-movflags", "+faststart", final]

        set_task(tid, state="downloading", stage="压缩中", progress=3)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace")
        PROCS[tid] = proc
        pct = 3
        for line in proc.stderr:
            m = re.search(r"time=(\d+):(\d+):(\d+\.?\d*)", line)
            if m and total:
                sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                pct = max(pct, min(97, 3 + int(sec / total * 94)))
                set_task(tid, progress=pct)
        proc.wait()
        PROCS.pop(tid, None)
        if tid in CANCELLED:
            CANCELLED.discard(tid)
            raise RuntimeError("已取消")

        if proc.returncode != 0 or not os.path.isfile(final):
            raise RuntimeError("压缩失败，检查视频是否可读")

        # 压完核对：时长、分辨率、帧率得对得上，否则宁可报错也不给坏文件
        ow, oh, of, od = _probe_video(ff, final)
        if od <= 0:
            raise RuntimeError("输出文件读不出来，压缩失败")
        if total > 2 and abs(od - total) / total > 0.15:
            raise RuntimeError(
                f"输出时长异常（源 {total:.1f}s，结果 {od:.1f}s），压缩可能中断了")
        if height and abs(oh - int(height)) > 4:
            raise RuntimeError(
                f"输出分辨率不对（要求 {height}p，实际 {oh}p），请重试")
        if fps and of > 0 and abs(of - float(fps)) > 2:
            raise RuntimeError(
                f"输出帧率不对（要求 {int(float(fps))}fps，实际 {of:.1f}fps），请重试")

        set_task(tid, state="done", progress=100, stage="完成",
                 file=os.path.basename(final),
                 size=os.path.getsize(final), note=note)
    except Exception as e:
        set_task(tid, state="error", stage=str(e)[:200], progress=100)


def _video_duration(ff, path, timeout=20):
    """从 ffmpeg 的 stderr 里抠出时长（没有 ffprobe 也能用）。"""
    try:
        r = subprocess.run([ff, "-i", path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr or "")
        if m:
            return (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                    + float(m.group(3)))
    except Exception:
        pass
    return 0


def run_job(tid, video_url, audio_url, mp4_url, name, kind="video"):
    with DL_SEM:
        _run_job(tid, video_url, audio_url, mp4_url, name, kind)


def _run_job(tid, video_url, audio_url, mp4_url, name, kind="video"):
    out = load_out()
    parts = os.path.join(out, ".parts")
    try:
        os.makedirs(parts, exist_ok=True)
        name = unique_base(out, name)
        ffmpeg = find_ffmpeg()

        # ---- 仅音频（mp3）----
        if kind == "mp3":
            src = audio_url or mp4_url
            if not src:
                raise RuntimeError("这条链接没有可提取的音轨")
            set_task(tid, state="downloading", stage="下载音频", progress=0)
            tmp = os.path.join(parts, f"{tid}.src")
            save_stream(src, tmp, lambda g, t: set_task(
                tid, progress=int(g * 80 / t) if t else 0))

            if ffmpeg:
                set_task(tid, stage="转码 MP3", progress=85)
                final = os.path.join(out, name + ".mp3")
                r = subprocess.run(
                    [ffmpeg, "-y", "-loglevel", "error", "-i", tmp, "-vn",
                     "-c:a", "libmp3lame", "-q:a", "2", final],
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
                os.remove(tmp)
                if r.returncode != 0:
                    raise RuntimeError((r.stderr or "转码失败")[-300:])
            else:
                # 没有 ffmpeg 就保留原始 AAC 容器，照样能播放
                final = os.path.join(out, name + ".m4a")
                os.replace(tmp, final)

            set_task(tid, state="done", progress=100, stage="完成",
                     file=os.path.basename(final))
            return

        final = os.path.join(out, name + ".mp4")

        if mp4_url and (not video_url or not ffmpeg):
            set_task(tid, state="downloading", stage="下载中", progress=0)
            tmp = os.path.join(parts, f"{tid}.mp4")
            save_stream(mp4_url, tmp, lambda g, t: set_task(
                tid, progress=int(g * 100 / t) if t else 0))
            os.replace(tmp, final)
            set_task(tid, state="done", progress=100, stage="完成",
                     file=os.path.basename(final))
            return

        vtmp = os.path.join(parts, f"{tid}.v")
        atmp = os.path.join(parts, f"{tid}.a")

        set_task(tid, state="downloading", stage="下载视频", progress=0)
        save_stream(video_url, vtmp, lambda g, t: set_task(
            tid, progress=int(g * 45 / t) if t else 0))

        if audio_url:
            set_task(tid, stage="下载音频", progress=45)
            save_stream(audio_url, atmp, lambda g, t: set_task(
                tid, progress=45 + (int(g * 45 / t) if t else 0)))

        set_task(tid, stage="合并", progress=90)
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", vtmp]
        if audio_url:
            cmd += ["-i", atmp]
        cmd += ["-c", "copy", final]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        for f in (vtmp, atmp):
            if os.path.exists(f):
                os.remove(f)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or "合并失败")[-300:])

        set_task(tid, state="done", progress=100, stage="完成",
                 file=os.path.basename(final))

    except Exception as e:
        set_task(tid, state="error", stage=str(e)[:200])
    finally:
        _clean_parts(parts)


# ============================================================ 双通道解析
# 国内平台先走原解析站（画质更稳），其余先走 yt-dlp（覆盖面广）
SITE_FIRST = ("bilibili", "b23.tv", "douyin", "iesdouyin", "xiaohongshu",
              "xhslink", "weibo", "kuaishou", "ixigua", "iqiyi", "youku",
              "v.qq.com", "douban", "zhihu", "toutiao")


def parse_via_site(url, extracted):
    try:
        links = Resolver().resolve(url)
    except Exception:
        links = []
    if not links:
        real = expand_short(url)
        if real:
            try:
                links = Resolver().resolve(real)
                url = real
            except Exception:
                links = []
    if not links:
        return None
    videos, audios, mp4s = classify(links)
    title = fetch_title(url)
    out = {"ok": True, "url": url, "extracted": extracted, "via": "site",
           "title": title or "", "videos": videos, "audios": audios,
           "mp4s": mp4s,
           "suggest": safe_name(title) if title else suggest_name(url),
           "best": (videos[0]["label"] if videos else
                    (mp4s[0]["label"] if mp4s else "未知"))}
    # 只有音频没有视频，多半是平台要 cookie 了，别让用户以为下到了正片
    if not videos and not mp4s:
        out["warning"] = ("只解析到音频，视频流没拿到。抖音、小红书这类平台现在需要 cookie，"
                          "先点「装插件」导出一次再导入") if audios else \
                         "没解析到可下载的内容，链接可能失效，或者这个平台需要 cookie"
    return out


def parse_via_ytdlp(url, extracted):
    info = ytdlp_probe(url)
    if not info:
        return None
    vids, auds = ytdlp_options(info)
    title = (info.get("title") or "").strip() or None
    out = {"ok": True, "url": url, "extracted": extracted, "via": "ytdlp",
           "title": title or "", "videos": vids, "audios": auds, "mp4s": [],
           "suggest": safe_name(title) if title else suggest_name(url),
           "best": vids[0]["label"] if vids else "未知"}
    if not vids and not auds:
        out["warning"] = ("这个平台没返回可下载的格式，通常是缺 cookie。"
                          "点「装插件」导出一次登录状态再试")
    return out


def _has_cookie_for(domain):
    return any(domain in d for d in _read_cookie_domains())


def _label_num(label):
    m = re.match(r"(\d+)", str(label or ""))
    return int(m.group(1)) if m else 0


def _pick_better(a, b):
    """两条通道都有结果时，选最高画质更高的那个。"""
    if not a:
        return b
    if not b:
        return a
    ha = max([_label_num(v.get("label")) for v in (a.get("videos") or [])] or [0])
    hb = max([_label_num(v.get("label")) for v in (b.get("videos") or [])] or [0])
    return a if ha >= hb else b


# ============================================================ HTTP
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, path, mime="video/mp4", name=None):
        """支持 Range 的发送，浏览器里 <video> 才能拖动进度条。"""
        size = os.path.getsize(path)
        start, end = 0, size - 1
        partial = False
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if name:
            self.send_header(
                "Content-Disposition",
                "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _file(self, path, name=None, mime="application/octet-stream"):
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        if name:
            self.send_header(
                "Content-Disposition",
                "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(262144)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _same_origin(self):
        """挡住其它网页对本服务的跨站请求（CSRF）。
        浏览器跨站 POST 一定会带 Origin，比对 Host 即可。
        自家扩展（chrome-extension://）放行。"""
        host = (self.headers.get("Host") or "").lower()
        org = (self.headers.get("Origin") or "")
        if org.startswith(("chrome-extension://", "moz-extension://")):
            return True
        for hdr in ("Origin", "Referer"):
            v = self.headers.get(hdr)
            if not v:
                continue
            try:
                netloc = urllib.parse.urlparse(v).netloc.lower()
            except Exception:
                return False
            if netloc and netloc != host:
                return False
        return True

    def _handle_upload(self, ctype):
        if "multipart/form-data" not in (ctype or "").lower():
            return self._json({"error": "需要 multipart/form-data"}, 400)
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._json({"error": "空文件"}, 400)
        if n > UPLOAD_MAX:
            return self._json(
                {"error": f"文件太大，上限 {UPLOAD_MAX // (1024 ** 3)} GB"}, 413)

        m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ctype)
        if not m:
            return self._json({"error": "缺少 boundary"}, 400)
        delim = b"--" + (m.group(1) or m.group(2)).strip().encode()

        # 流式写盘：大文件不能整个读进内存
        tmp = os.path.join(upload_dir(), f".incoming_{int(time.time() * 1000)}")
        fh = None
        buf = b""
        got = 0
        state = "header"
        fn = None
        try:
            while got < n:
                chunk = self.rfile.read(min(256 * 1024, n - got))
                if not chunk:
                    break
                got += len(chunk)
                buf += chunk

                if state == "header":
                    idx = buf.find(b"\r\n\r\n")
                    if idx < 0:
                        if len(buf) > 65536:      # header 不该这么长
                            raise RuntimeError("表单格式不对")
                        continue
                    head = buf[:idx].decode("utf-8", "replace")
                    fm = re.search(r'filename="([^"]*)"', head)
                    if not fm:
                        raise RuntimeError("没找到文件字段")
                    fn = fm.group(1)
                    buf = buf[idx + 4:]
                    fh = open(tmp, "wb")
                    state = "body"

                if state == "body":
                    tail = b"\r\n" + delim
                    idx = buf.find(tail)
                    if idx >= 0:
                        fh.write(buf[:idx])
                        state = "done"
                        break
                    # 末尾可能是半个 boundary，留着下次再判断
                    keep = len(tail) - 1
                    if len(buf) > keep:
                        fh.write(buf[:-keep])
                        buf = buf[-keep:]

            if state != "done":
                raise RuntimeError("上传中断，文件不完整")
        except Exception as e:
            if fh:
                fh.close()
            try:
                os.remove(tmp)
            except Exception:
                pass
            return self._json({"error": f"上传失败：{e}"}, 500)
        finally:
            if fh and not fh.closed:
                fh.close()

        try:
            size = os.path.getsize(tmp)
            if size <= 0:
                os.remove(tmp)
                return self._json({"error": "文件是空的"}, 400)
            name = safe_upload_name(fn)
            stem, ext = os.path.splitext(name)
            dest = os.path.join(upload_dir(), name)
            i = 2
            while os.path.exists(dest):
                dest = os.path.join(upload_dir(), f"{stem} ({i}){ext}")
                i += 1
            shutil.move(tmp, dest)
        except Exception as e:
            return self._json({"error": f"保存失败：{e}"}, 500)
        return self._json({"ok": True, "name": os.path.basename(dest),
                           "size": size})

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(p.query)

        if p.path in ("/", "/index.html"):
            html = os.path.join(RES_DIR, "index.html")
            return self._file(html, None, "text/html; charset=utf-8")

        if p.path == "/api/uploads":
            try:
                d = upload_dir()
                items, total = [], 0
                for n in os.listdir(d):
                    fp = os.path.join(d, n)
                    if not os.path.isfile(fp) or n.startswith("."):
                        continue
                    sz = os.path.getsize(fp)
                    total += sz
                    items.append({"name": n, "size": sz,
                                  "mtime": int(os.path.getmtime(fp))})
                items.sort(key=lambda x: -x["mtime"])
                return self._json({"files": items, "total": total, "dir": d})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if p.path == "/api/tasks":
            # 刷新页面后用来恢复正在跑的任务
            with LOCK:
                live = [dict(id=k, **v) for k, v in TASKS.items()
                        if v.get("state") not in ("done", "error")]
            live.sort(key=lambda x: x.get("progress") or 0)
            return self._json({"tasks": live})

        if p.path == "/api/env":
            return self._json({"ffmpeg": bool(find_ffmpeg()), "out": load_out(),
                               "default": DEFAULT_OUT,
                               "places": common_places(),
                               "upload_max_gb": round(UPLOAD_MAX_GB, 1),
                               "ram_total_gb": round(RAM_TOTAL_GB, 1),
                               "ram_free_gb": round(RAM_AVAIL_GB, 1)})

        if p.path == "/api/pick-dir":
            # 对话框只能在有桌面会话的进程里弹；无桌面时会永久阻塞，所以加超时。
            box = {}
            t = threading.Thread(target=lambda: box.update(p=pick_dir_dialog()),
                                 daemon=True)
            t.start()
            t.join(75)
            if t.is_alive():
                return self._json({"ok": False,
                                   "hint": "选择框没响应，直接在输入框里改路径就行"})
            pth = box.get("p")
            if not pth:
                return self._json({"ok": False,
                                   "hint": "已取消，或在输入框里直接改路径"})
            try:
                os.makedirs(pth, exist_ok=True)
            except Exception as e:
                return self._json({"ok": False, "error": f"目录不可用：{e}"})
            save_out(pth)
            return self._json({"ok": True, "out": pth})

        if p.path == "/api/cookies":
            return self._json(cookie_status())

        if p.path == "/api/extension":
            path = ensure_extension_dir()
            return self._json({"ok": bool(path), "path": path or ""})

        if p.path == "/api/platforms":
            return self._json({"platforms": [
                {"key": k, "name": n, "url": u} for k, n, u, _ in PLATFORMS]})

        if p.path == "/api/clipboard":
            return self._json({"text": read_clipboard_text() or ""})

        if p.path == "/api/browsers":
            return self._json({"browsers": browser_status()})

        if p.path == "/api/connect/status":
            logged, total = connected_platforms()
            return self._json({"cdp": _cdp_alive(), "total": total,
                               "connected": logged})

        if p.path == "/api/files":
            out = load_out()
            os.makedirs(out, exist_ok=True)
            items = []
            for fn in os.listdir(out):
                if fn.startswith(".") or not fn.endswith((".mp4", ".m4s", ".mp3", ".m4a")):
                    continue
                fp = os.path.join(out, fn)
                try:
                    items.append({"name": fn, "size": os.path.getsize(fp),
                                  "mtime": int(os.path.getmtime(fp))})
                except OSError:
                    pass
            items.sort(key=lambda x: -x["mtime"])
            return self._json({"files": items, "out": out})

        if p.path == "/api/task":
            tid = (q.get("id") or [""])[0]
            with LOCK:
                t = dict(TASKS.get(tid) or {})
            if not t:
                return self._json({"error": "not found"}, 404)
            return self._json(t)

        if p.path == "/preview":
            fn = os.path.basename((q.get("n") or [""])[0])
            fp = os.path.join(upload_dir(), fn)
            if not os.path.isfile(fp):
                return self._json({"error": "not found"}, 404)
            ext = os.path.splitext(fn)[1].lower()
            mime = {".mp4": "video/mp4", ".m4v": "video/mp4",
                    ".webm": "video/webm", ".mov": "video/quicktime",
                    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
                    ".ts": "video/mp2t", ".flv": "video/x-flv",
                    ".wmv": "video/x-ms-wmv"}.get(ext, "application/octet-stream")
            return self._stream(fp, mime)

        if p.path == "/file":
            fn = os.path.basename((q.get("n") or [""])[0])
            fp = os.path.join(load_out(), fn)
            if not os.path.isfile(fp):
                return self._json({"error": "not found"}, 404)
            return self._file(fp, fn)

        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._same_origin():
            return self._json({"error": "blocked: cross-origin request"}, 403)
        p = urllib.parse.urlparse(self.path)
        ctype_raw = self.headers.get("Content-Type") or ""
        ctype = ctype_raw.lower()

        # 文件上传走 multipart，其余接口仍然只收 JSON
        if p.path == "/api/upload":
            return self._handle_upload(ctype_raw)

        # 浏览器跨站请求若用 text/plain 可绕过 CORS 预检，这里强制要求 JSON
        if not ctype.startswith("application/json"):
            return self._json({"error": "content-type must be application/json"}, 415)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)

        if p.path == "/api/parse":
            raw = (body.get("url") or "").strip()
            if not raw:
                return self._json({"error": "先贴个链接"}, 400)
            url, extracted = extract_url(raw)
            if not url:
                return self._json(
                    {"error": "没认出链接。整段分享文字直接贴进来就行，我会自己找。"}, 400)
            if not _url_allowed(url):
                return self._json(
                    {"error": "只接受公开的 http/https 链接，内网地址不能解析"}, 400)

            # 按域名决定先用哪条通道
            host = (urllib.parse.urlparse(url).netloc or "").lower()
            site_first = any(k in host for k in SITE_FIRST)

            if site_first:
                result = parse_via_site(url, extracted) or parse_via_ytdlp(url, extracted)
                # B 站登录态下 yt-dlp 能给 1080P，解析站固定 480P。
                # 只有在 cookies.txt 里真有 bilibili 时才多跑一次 yt-dlp，取高的。
                if "bilibili" in host and _has_cookie_for("bilibili.com"):
                    alt = parse_via_ytdlp(url, extracted)
                    result = _pick_better(result, alt)
            else:
                result = parse_via_ytdlp(url, extracted) or parse_via_site(url, extracted)

            if not result:
                if _need_cookie(url):
                    return self._json({
                        "error": "这个站点要登录才能取到视频。用浏览器扩展导出 cookies.txt "
                                 "放到程序目录（和 server.py 同一层）再试。"
                    }, 200)
                return self._json(
                    {"error": "这个链接没解析出可下载的内容，换个链接试试"}, 200)
            return self._json(result)
        if p.path == "/api/cookies":
            text = body.get("content") or ""
            if not text.strip():
                return self._json({"error": "文件是空的"}, 400)
            if not _valid_cookie_text(text):
                return self._json(
                    {"error": "这看着不是 cookies.txt。要用 Netscape 格式的导出文件。"}, 400)
            try:
                old = ""
                if os.path.isfile(COOKIE_FILE):
                    with open(COOKIE_FILE, encoding="utf-8", errors="replace") as f:
                        old = f.read()
                merged = _merge_cookie_text(old, text)
                with open(COOKIE_FILE, "w", encoding="utf-8", newline="\n") as f:
                    f.write(merged)
            except Exception as e:
                return self._json({"error": f"写入失败：{e}"}, 500)
            return self._json({"ok": True, "status": cookie_status()})

        if p.path == "/api/cookies/raw":
            items = body.get("cookies") or []
            if not isinstance(items, list) or not items:
                return self._json({"error": "没有收到 cookie"}, 400)
            text = _chrome_cookies_to_netscape(items)
            if not text.strip("# Netscape HTTP Cookie File\n"):
                return self._json({"error": "cookie 格式不对"}, 400)
            try:
                old = ""
                if os.path.isfile(COOKIE_FILE):
                    with open(COOKIE_FILE, encoding="utf-8", errors="replace") as f:
                        old = f.read()
                with open(COOKIE_FILE, "w", encoding="utf-8", newline="\n") as f:
                    f.write(_merge_cookie_text(old, text))
            except Exception as e:
                return self._json({"error": f"写入失败：{e}"}, 500)
            return self._json({"ok": True, "count": len(items),
                               "status": cookie_status()})

        if p.path == "/api/uploads/delete":
            d = upload_dir()
            if body.get("all"):
                names = [n for n in os.listdir(d)
                         if os.path.isfile(os.path.join(d, n)) and not n.startswith(".")]
            else:
                names = body.get("names") or []
            freed, removed = 0, []
            for n in names:
                safe = os.path.basename(str(n))     # 防路径穿越
                fp = os.path.join(d, safe)
                if os.path.isfile(fp):
                    try:
                        freed += os.path.getsize(fp)
                        os.remove(fp)
                        removed.append(safe)
                    except OSError:
                        pass
            return self._json({"ok": True, "removed": removed, "freed": freed})

        if p.path == "/api/cancel":
            tid = str(body.get("id") or "")
            if _kill_task(tid):
                set_task(tid, state="error", stage="已取消", progress=100)
                return self._json({"ok": True})
            t = TASKS.get(tid) or {}
            if t.get("state") in ("done", "error"):
                return self._json({"error": "任务已结束"}, 400)
            return self._json({"error": "任务不在运行"}, 400)

        if p.path == "/api/trim":
            fn = os.path.basename(body.get("file") or "")
            src = os.path.join(upload_dir(), fn)
            if not fn or not os.path.isfile(src):
                return self._json({"error": "先上传一个视频"}, 400)
            base = os.path.splitext(fn)[0] + "_clip"
            tid = hashlib.md5(f"t{time.time()}{fn}".encode()).hexdigest()[:12]
            set_task(tid, state="queued", stage="排队中", progress=0, kind="video")
            threading.Thread(
                target=trim_job,
                args=(tid, src, load_out(), base,
                      body.get("start"), body.get("duration"),
                      body.get("mode") or "reencode"),
                daemon=True).start()
            return self._json({"id": tid})

        if p.path == "/api/audio":
            fn = os.path.basename(body.get("file") or "")
            src = os.path.join(upload_dir(), fn)
            if not fn or not os.path.isfile(src):
                return self._json({"error": "先上传一个视频"}, 400)
            base = os.path.splitext(fn)[0]
            tid = hashlib.md5(f"a{time.time()}{fn}".encode()).hexdigest()[:12]
            set_task(tid, state="queued", stage="排队中", progress=0, kind="video")
            threading.Thread(
                target=audio_job,
                args=(tid, src, load_out(), base,
                      body.get("format") or "mp3",
                      body.get("start"), body.get("duration")),
                daemon=True).start()
            return self._json({"id": tid})

        if p.path == "/api/convert":
            fn = os.path.basename(body.get("file") or "")
            src = os.path.join(upload_dir(), fn)
            if not fn or not os.path.isfile(src):
                return self._json({"error": "先上传一个视频"}, 400)
            base = os.path.splitext(fn)[0]
            tid = hashlib.md5(f"v{time.time()}{fn}".encode()).hexdigest()[:12]
            set_task(tid, state="queued", stage="排队中", progress=0, kind="video")
            threading.Thread(
                target=convert_job,
                args=(tid, src, load_out(), base,
                      body.get("target") or "mp4"),
                daemon=True).start()
            return self._json({"id": tid})

        if p.path == "/api/compress":
            fn = os.path.basename(body.get("file") or "")
            src = os.path.join(upload_dir(), fn)
            if not fn or not os.path.isfile(src):
                return self._json({"error": "先上传一个视频"}, 400)
            base = os.path.splitext(fn)[0] + "_compressed"
            tid = hashlib.md5(f"c{time.time()}{fn}".encode()).hexdigest()[:12]
            set_task(tid, state="queued", stage="排队中", progress=0, kind="video")
            threading.Thread(
                target=compress_video_job,
                args=(tid, src, load_out(), base,
                      body.get("height"), body.get("fps"),
                      body.get("crf"), body.get("target_mb")),
                daemon=True).start()
            return self._json({"id": tid})

        if p.path == "/api/togif":
            fn = os.path.basename(body.get("file") or "")
            src = os.path.join(upload_dir(), fn)
            if not fn or not os.path.isfile(src):
                return self._json({"error": "先上传一个视频"}, 400)
            base = os.path.splitext(fn)[0]
            tid = hashlib.md5(f"{time.time()}{fn}".encode()).hexdigest()[:12]
            set_task(tid, state="queued", stage="排队中", progress=0, kind="gif")
            threading.Thread(
                target=to_gif_job,
                args=(tid, src, load_out(), base,
                      body.get("fps"), body.get("width"),
                      body.get("start"), body.get("duration"),
                      body.get("target_kb")),
                daemon=True).start()
            return self._json({"id": tid})

        if p.path == "/api/extension/store":
            exe = _find_browser_exe("Edge")
            want = (body.get("browser") or "").lower()
            url = EXT_STORE_EDGE if want == "edge" else EXT_STORE_CHROME
            ok = open_in_browser(url)
            return self._json({"ok": ok, "url": url,
                               "fallback": EXT_STORE_SEARCH,
                               "error": None if ok else "打不开浏览器"})

        if p.path == "/api/extension/open":
            path = ensure_extension_dir()
            ok = open_extension_page()
            return self._json({"ok": ok, "path": path or "",
                               "error": None if ok else "没找到 Chrome 或 Edge"})

        if p.path == "/api/connect":
            key = body.get("platform") or ""
            home = next((u for k, _n, u, _d in PLATFORMS if k == key), None)
            if not home:
                return self._json({"error": "未知平台"}, 400)
            ok, msg = ensure_login_browser(home)
            if not ok:
                return self._json({"error": msg or "浏览器启动失败"}, 500)
            return self._json({"ok": True, "url": home})

        if p.path == "/api/connect/save":
            key = body.get("platform") or ""
            n = save_platform_cookies(key)
            if not n:
                return self._json({"error": "这个平台还没有 cookie，先登录"}, 200)
            return self._json({"ok": True, "saved": n, "status": cookie_status()})

        if p.path == "/api/open":
            url = (body.get("url") or "").strip()
            if not _url_allowed(url):
                return self._json({"error": "链接不合法"}, 400)
            return self._json({"ok": open_in_browser(url)})

        if p.path == "/api/cookies/auto":
            if not AUTO_LOCK.acquire(blocking=False):
                return self._json({"error": "正在获取中，请稍候"}, 429)
            try:
                host = self.headers.get("Host") or f"127.0.0.1:{PORT}"
                reopen = f"http://{host}/"
                only = body.get("browser") or None
                if only not in (None, "Chrome", "Edge"):
                    only = None
                text, browser, notes = auto_grab_cookies(reopen_url=reopen, only=only)
                if not text:
                    return self._json({
                        "error": "自动获取失败",
                        "notes": notes,
                        "hint": "需要至少有一个浏览器（Chrome / Edge）当前没在运行。"
                                "先把它完全退出（含后台进程），再点一次。",
                    }, 200)
                old = ""
                if os.path.isfile(COOKIE_FILE):
                    with open(COOKIE_FILE, encoding="utf-8", errors="replace") as f:
                        old = f.read()
                merged = _merge_cookie_text(old, text)
                with open(COOKIE_FILE, "w", encoding="utf-8", newline="\n") as f:
                    f.write(merged)
                return self._json({"ok": True, "browser": browser,
                                   "notes": notes, "status": cookie_status()})
            except Exception as e:
                return self._json({"error": f"获取失败：{e}"}, 500)
            finally:
                AUTO_LOCK.release()

        if p.path == "/api/config":
            raw = (body.get("out") or "").strip().strip('"').strip("'")
            if not raw:
                return self._json({"error": "路径不能为空"}, 400)
            path = os.path.expandvars(os.path.expanduser(raw))
            if not os.path.isabs(path):
                path = os.path.join(DATA_DIR, path)
            path = os.path.normpath(path)
            try:
                os.makedirs(path, exist_ok=True)
                if not os.access(path, os.W_OK):
                    raise PermissionError("没有写入权限")
                save_out(path)
                return self._json({"ok": True, "out": path})
            except Exception as e:
                return self._json({"error": f"这个路径不能用：{e}"}, 400)

        if p.path == "/api/grab":
            video_url = body.get("video_url")
            audio_url = body.get("audio_url")
            mp4_url = body.get("mp4_url")
            via = body.get("via") or "site"
            kind = body.get("kind") or "video"
            if kind not in ("video", "mp3"):
                kind = "video"
            name = safe_name(body.get("name") or "video")

            if via == "ytdlp":
                src = body.get("url")
                if not src:
                    return self._json({"error": "缺少视频链接"}, 400)
                try:
                    height = int(body.get("height") or 0)
                except (TypeError, ValueError):
                    height = 0
                tid = uuid.uuid4().hex[:12]
                set_task(tid, state="queued", stage="排队中", progress=0,
                         kind=kind, ext=".mp3" if kind == "mp3" else ".mp4")
                threading.Thread(target=run_ytdlp_job, daemon=True,
                                 args=(tid, src, name, kind, height)).start()
                return self._json({"id": tid, "kind": kind})

            if not (video_url or mp4_url or audio_url):
                return self._json({"error": "缺少媒体地址"}, 400)
            tid = uuid.uuid4().hex[:12]
            set_task(tid, state="queued", stage="排队中", progress=0,
                     kind=kind, ext=".mp3" if kind == "mp3" else ".mp4")
            threading.Thread(target=run_job, daemon=True,
                             args=(tid, video_url, audio_url, mp4_url, name, kind)).start()
            return self._json({"id": tid, "kind": kind})

        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        if not self._same_origin():
            return self._json({"error": "blocked: cross-origin request"}, 403)
        p = urllib.parse.urlparse(self.path)
        if p.path == "/api/cookies":
            try:
                if os.path.isfile(COOKIE_FILE):
                    os.remove(COOKIE_FILE)
                return self._json({"ok": True, "status": cookie_status()})
            except Exception as e:
                return self._json({"error": f"删除失败：{e}"}, 500)
        return self._json({"error": "not found"}, 404)


def safe_name(s, maxlen=90):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", s).strip(" .")
    return s[:maxlen] or "video"


def suggest_name(url):
    u = urllib.parse.urlparse(url)
    host = (u.netloc or "").lower().replace("www.", "")
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if m:
        return "bilibili_" + m.group(1)
    seg = [s for s in u.path.split("/") if s]
    tail = re.sub(r"[^\w\-.]", "", seg[-1])[:24] if seg else ""
    site = host.split(".")[0] or "video"
    return f"{site}_{tail}" if tail else site


class Server(socketserver.ThreadingTCPServer):
    # Windows 上 SO_REUSEADDR 会允许两个进程绑同一端口，
    # 导致请求被旧进程接走，必须关掉。
    allow_reuse_address = (sys.platform != "win32")
    daemon_threads = True


def main():
    if sys.platform == "win32":
        try:
            os.system("chcp 65001 >nul")
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    # 启动时清一次上传缓存，之后每小时清一次
    _clean_uploads()
    threading.Thread(target=_upload_janitor, daemon=True).start()

    port = PORT
    for _ in range(12):
        try:
            httpd = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            # 首选端口被占，先确认是不是自己已经在跑
            if port == PORT:
                try:
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{PORT}/api/env", timeout=2) as r:
                        json.load(r)
                    print(f"已有实例在运行：http://127.0.0.1:{PORT}")
                    if "--no-browser" not in sys.argv:
                        import webbrowser
                        webbrowser.open(f"http://127.0.0.1:{PORT}")
                    return 0
                except Exception:
                    pass
            port += 1
    else:
        print("端口被占满，放弃")
        return 1

    os.makedirs(load_out(), exist_ok=True)
    ext = ensure_extension_dir()
    ff = find_ffmpeg()
    print("=" * 56)
    print(f"  本地视频提取  →  http://127.0.0.1:{port}")
    print(f"  数据目录      →  {DATA_DIR}")
    print(f"  保存目录      →  {load_out()}")
    print(f"  ffmpeg        →  {ff or '未找到（将只下载单文件低码率版）'}")
    print(f"  yt-dlp        →  {'已就绪（支持 YouTube 等 1800+ 站点）' if _ytdlp_available() else '未安装，仅国内平台可用'}")
    print(f"  cookies.txt   →  {'已就绪' if os.path.isfile(COOKIE_FILE) else '未提供（X / Vimeo 等需登录的站点用不了）'}")
    print(f"  浏览器扩展    →  {ext or '未找到'}")
    print("=" * 56)

    if "--no-browser" not in sys.argv:
        import webbrowser
        threading.Timer(0.9, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
