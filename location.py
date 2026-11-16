#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iPhone 定位模拟控制台 —— 单文件本地 Web 服务，零第三方依赖。

前置（只需做一次）：
    brew install python@3.12
    python3.12 -m venv ~/.venv-pmd3 && ~/.venv-pmd3/bin/pip install -U pymobiledevice3

设备侧：设置 → 隐私与安全性 → 开发者模式 → 打开并重启；插线后点「信任」。

启动：
    python3 location.py
    浏览器打开 http://127.0.0.1:8788

实现要点：pymobiledevice3 11.x 的 `simulate-location set` 是常驻会话进程，
只有进程存活期间定位才被接管，因此本服务持有一个长驻子进程来「保活」。
iOS 17+ 无需 root，工具会自动回退到用户态隧道。
"""

import argparse
import atexit
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8788
PMD3_CANDIDATES = [
    os.path.expanduser("~/.venv-pmd3/bin/pymobiledevice3"),
    os.path.expanduser("~/.venv-pmd3/bin/pymobiledevice3.exe"),
]
ENV = {**os.environ, "LC_ALL": "en_US.UTF-8", "PYTHONUNBUFFERED": "1"}

_lock = threading.RLock()
_state = {"lat": None, "lng": None, "playing": False, "error": None}
_session = None          # 当前持有的 set / play 子进程
_session_file = None     # play 用的临时 GPX
_dev_cache = {"at": 0.0, "value": None}


# ---------------------------------------------------------------- 工具链


def find_pmd3():
    for path in PMD3_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return shutil.which("pymobiledevice3")


def run(args, timeout=25):
    """执行短命令，返回 (ok, stdout)。"""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=ENV)
        return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return False, "命令超时（%ss）" % timeout
    except FileNotFoundError:
        return False, "找不到可执行文件：%s" % args[0]


def _parse_json(text):
    start = text.find("[")
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except ValueError:
        return None


def discover(pmd3):
    """发现设备：USB 优先，其次无线；失败返回 None。带缓存。"""
    now = time.time()
    if now - _dev_cache["at"] < 6:
        return _dev_cache["value"]

    result = None
    ok, out = run([pmd3, "usbmux", "list"], timeout=8)
    rows = _parse_json(out) if ok else None
    if rows:
        d = rows[0]
        result = {
            "name": d.get("DeviceName") or "iPhone",
            "model": d.get("ProductType") or "",
            "os": d.get("ProductVersion") or "",
            "connection": d.get("ConnectionType") or "",
        }
    else:
        ok, out = run([pmd3, "remote", "browse"], timeout=12)
        rows = _parse_json(out) if ok else None
        if rows:
            d = rows[0]
            result = {
                "name": d.get("name") or "iPhone",
                "model": d.get("model") or "",
                "os": "",
                "connection": "",
            }

    _dev_cache["at"] = now
    _dev_cache["value"] = result
    return result


# ---------------------------------------------------------------- 会话管理


def session_alive():
    with _lock:
        return _session is not None and _session.poll() is None


def _cleanup_file():
    global _session_file
    if _session_file:
        try:
            os.unlink(_session_file)
        except OSError:
            pass
        _session_file = None


def stop_session():
    """结束当前模拟会话（会释放定位接管，设备回到真实位置）。"""
    global _session
    with _lock:
        proc, _session = _session, None
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
    _cleanup_file()
    _state["playing"] = False


def _start_session(args, settle=2.5):
    """启动常驻会话子进程；若它在 settle 秒内退出，说明启动失败。"""
    global _session, _session_file
    stop_session()

    fd, log_path = tempfile.mkstemp(suffix=".log")
    os.close(fd)
    out = open(log_path, "wb")
    try:
        proc = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, env=ENV)
    finally:
        out.close()

    time.sleep(settle)
    if proc.poll() is not None:
        raw = ""
        try:
            with open(log_path, "r", errors="replace") as fh:
                raw = fh.read()
        except OSError:
            pass
        try:
            os.unlink(log_path)
        except OSError:
            pass
        return None, _friendly(raw)

    with _lock:
        _session = proc
    try:
        os.unlink(log_path)
    except OSError:
        pass
    return proc, None


def _friendly(raw):
    text = (raw or "").strip()
    low = text.lower()
    if "developer" in low and "mode" in low:
        return "iPhone 未开启开发者模式：设置 → 隐私与安全性 → 开发者模式，开启后重启"
    if "pair" in low and ("trust" in low or "invalid" in low):
        return "设备未信任本机：拔插数据线，在 iPhone 上点「信任」"
    if "no device" in low or "not connected" in low or "usbmux" in low and "error" in low:
        return "未发现可用设备：确认已插线并信任，或与 Mac 处于同一 Wi-Fi"
    for line in reversed(text.splitlines()):
        line = re.sub(r"^\d{4}-\d\d-\d\d [\d:]+ \S+ \S+\[\d+\] ", "", line).strip()
        if not line or line.startswith("INFO"):
            continue
        return line[:300]
    return "命令执行失败"


# ---------------------------------------------------------------- 历史记录

HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "location_history.json")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "location_config.json")
HISTORY_LIMIT = 50


def _load_history():
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            rows = [
                d for d in data
                if isinstance(d, dict) and isinstance(d.get("lat"), (int, float)) and isinstance(d.get("lng"), (int, float))
            ][:HISTORY_LIMIT]
            for row in rows:
                row.setdefault("id", uuid.uuid4().hex[:8])
            return rows
    except (OSError, ValueError):
        pass
    return []


_history = _load_history()


def _save_history():
    tmp = HISTORY_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_history, fh, ensure_ascii=False)
        os.replace(tmp, HISTORY_PATH)
    except OSError:
        pass


def record_history(lat, lng, kind="set"):
    """只在真正下发成功后调用；同一坐标不重复占位，只提到最前并刷新时间。"""
    with _lock:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        for idx, item in enumerate(_history):
            if abs(item["lat"] - lat) < 1e-6 and abs(item["lng"] - lng) < 1e-6:
                item["at"] = stamp
                item["kind"] = kind
                if idx:
                    _history.insert(0, _history.pop(idx))
                _save_history()
                return
        _history.insert(0, {
            "id": uuid.uuid4().hex[:8], "lat": round(lat, 6), "lng": round(lng, 6),
            "at": stamp, "kind": kind,
        })
        del _history[HISTORY_LIMIT:]
        _save_history()


def api_history(_body=None):
    with _lock:
        return 200, {"ok": True, "items": list(_history)}


def api_history_delete(body):
    rid = (body or {}).get("id")
    with _lock:
        for idx, item in enumerate(_history):
            if item.get("id") == rid:
                del _history[idx]
                _save_history()
                return 200, {"ok": True}
    return 404, {"ok": False, "error": "记录不存在"}


# ---------------------------------------------------------------- 地址搜索

def _load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_config = _load_config()


def amap_key():
    return (_config.get("amap_key") or os.environ.get("AMAP_KEY") or "").strip()


def api_config(body):
    key = ((body or {}).get("amap_key") or "").strip()
    _config["amap_key"] = key
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(_config, fh, ensure_ascii=False)
    except OSError:
        return 500, {"ok": False, "error": "配置写入失败"}
    return 200, {"ok": True, "configured": bool(key)}


def api_geocode(query):
    keyword = (query or "").strip()
    if not keyword:
        return 400, {"ok": False, "error": "请输入地址关键词"}
    key = amap_key()
    if not key:
        return 400, {"ok": False, "need_key": True, "error": "地址搜索需要高德 Web 服务 Key"}

    url = "https://restapi.amap.com/v3/place/text?" + urllib.parse.urlencode({
        "key": key, "keywords": keyword, "offset": 10, "page": 1, "extensions": "base",
    })
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return 502, {"ok": False, "error": "地址服务请求失败：%s" % exc}
    if str(data.get("status")) != "1":
        return 502, {"ok": False, "error": "高德返回：%s" % (data.get("info") or "未知错误")}

    def text(value):
        return value if isinstance(value, str) else ""

    items = []
    for poi in data.get("pois") or []:
        loc = text(poi.get("location"))
        if "," not in loc:
            continue
        lng_s, lat_s = loc.split(",")[:2]
        try:
            lat, lng = float(lat_s), float(lng_s)
        except ValueError:
            continue
        addr = "".join([
            text(poi.get("pname")), text(poi.get("cityname")),
            text(poi.get("adname")), text(poi.get("address")),
        ])
        items.append({
            "name": text(poi.get("name")) or keyword,
            "addr": addr,
            "lat": lat, "lng": lng, "crs": "gcj02",
        })
        if len(items) >= 8:
            break
    return 200, {"ok": True, "items": items}


# ---------------------------------------------------------------- 接口


def api_status(_body=None):
    pmd3 = find_pmd3()
    elapsed = 0.0
    if _state["playing"] and _play_started["at"]:
        elapsed = round(time.time() - _play_started["at"], 1)
    payload = {
        "ok": True,
        "pmd3_ready": bool(pmd3),
        "device": discover(pmd3) if pmd3 else None,
        "session": session_alive(),
        "playing": _state["playing"],
        "elapsed": elapsed,
        "simulated": None if _state["lat"] is None else {"lat": _state["lat"], "lng": _state["lng"]},
        "error": _state["error"],
        "install": (
            "brew install python@3.12\n"
            "python3.12 -m venv ~/.venv-pmd3\n"
            "~/.venv-pmd3/bin/pip install -U pymobiledevice3"
        ),
    }
    return 200, payload


def api_location(body):
    pmd3 = find_pmd3()
    if not pmd3:
        return 400, {"ok": False, "error": "未安装 pymobiledevice3"}
    try:
        lat = float(body["lat"])
        lng = float(body["lng"])
    except (KeyError, TypeError, ValueError):
        return 400, {"ok": False, "error": "经纬度格式不对"}
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        return 400, {"ok": False, "error": "经纬度超出有效范围"}

    _state["playing"] = False
    proc, err = _start_session(
        [pmd3, "developer", "dvt", "simulate-location", "set", "--", "%.7f" % lat, "%.7f" % lng]
    )
    if not proc:
        _state["error"] = err
        return 502, {"ok": False, "error": err}

    with _lock:
        _state["lat"], _state["lng"], _state["error"] = lat, lng, None
    record_history(lat, lng, "set")
    return 200, {"ok": True, "lat": lat, "lng": lng}


def api_clear(_body):
    pmd3 = find_pmd3()
    if not pmd3:
        return 400, {"ok": False, "error": "未安装 pymobiledevice3"}
    stop_session()
    ok, out = run([pmd3, "developer", "dvt", "simulate-location", "clear"], timeout=30)
    with _lock:
        _state["lat"], _state["lng"] = None, None
    if not ok:
        _state["error"] = _friendly(out)
        return 502, {"ok": False, "error": _state["error"]}
    _state["error"] = None
    return 200, {"ok": True}


_play_started = {"at": 0.0}


def api_route(body):
    pmd3 = find_pmd3()
    if not pmd3:
        return 400, {"ok": False, "error": "未安装 pymobiledevice3"}
    points = body.get("points") or []
    try:
        points = [[float(p[0]), float(p[1])] for p in points]
    except (TypeError, ValueError, IndexError):
        return 400, {"ok": False, "error": "路径点格式不对"}
    if len(points) < 2:
        return 400, {"ok": False, "error": "至少需要 2 个途经点"}
    if len(points) > 200:
        return 400, {"ok": False, "error": "途经点最多 200 个"}
    try:
        speed = float(body.get("speed") or 15)
    except (TypeError, ValueError):
        speed = 15.0
    speed = max(1.0, min(speed, 300.0))

    global _session_file
    fd, path = tempfile.mkstemp(suffix=".gpx")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(_build_gpx(points, speed))

    proc, err = _start_session([pmd3, "developer", "dvt", "simulate-location", "play", path])
    if not proc:
        try:
            os.unlink(path)
        except OSError:
            pass
        _state["error"] = err
        return 502, {"ok": False, "error": err}

    _session_file = path
    seconds = _route_seconds(points, speed)
    with _lock:
        _state["lat"], _state["lng"] = points[-1][0], points[-1][1]
        _state["playing"] = True
        _state["error"] = None
    _play_started["at"] = time.time()
    record_history(points[-1][0], points[-1][1], "route")
    # play 播完后进程仍常驻（保持在终点），这里按推算时长收起「播放中」状态
    threading.Timer(seconds + 3, _end_playback).start()
    return 200, {"ok": True, "points": len(points), "seconds": round(seconds, 1)}


def _end_playback():
    with _lock:
        _state["playing"] = False


# ---------------------------------------------------------------- 路径计算


def _haversine(a, b):
    r = 6371000.0
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _route_seconds(points, kmh):
    total = sum(_haversine(points[i], points[i + 1]) for i in range(len(points) - 1))
    return total / (kmh / 3.6)


def _build_gpx(points, kmh):
    mps = kmh / 3.6
    start = datetime.now(timezone.utc)
    elapsed = 0.0
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="location.py" xmlns="http://www.topografix.com/GPX/1/1">',
        "<trk><name>route</name><trkseg>",
    ]
    for idx, point in enumerate(points):
        stamp = start + timedelta(seconds=elapsed)
        lines.append(
            '  <trkpt lat="%.7f" lon="%.7f"><time>%s</time></trkpt>'
            % (point[0], point[1], stamp.strftime("%Y-%m-%dT%H:%M:%SZ"))
        )
        if idx < len(points) - 1:
            elapsed += _haversine(point, points[idx + 1]) / mps
    lines += ["</trkseg></trk>", "</gpx>", ""]
    return "\n".join(lines)


atexit.register(stop_session)


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "LocationConsole/1.1"
    routes = {
        "/api/location": api_location,
        "/api/clear": api_clear,
        "/api/route": api_route,
        "/api/history/delete": api_history_delete,
        "/api/config": api_config,
    }

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/api/status"):
            code, payload = api_status()
            self._json(code, payload)
        elif self.path.startswith("/api/geocode"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code, payload = api_geocode((qs.get("q") or [""])[0])
            self._json(code, payload)
        elif self.path.startswith("/api/history"):
            code, payload = api_history()
            self._json(code, payload)
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?")[0]
        handler = self.routes.get(path)
        if not handler:
            self._json(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
            return
        try:
            code, payload = handler(body if isinstance(body, dict) else {})
        except Exception as exc:  # 兜底，避免单个请求打断服务
            code, payload = 500, {"ok": False, "error": str(exc)}
        self._json(code, payload)

    def _json(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, raw, "application/json; charset=utf-8")

    def _send(self, code, raw, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        pass


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iPhone 定位模拟</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font: 14px/1.5 -apple-system, "SF Pro Text", "PingFang SC", system-ui, sans-serif;
    color: #171717; background: #f5f5f6; display: flex; overflow: hidden;
  }
  #side {
    width: 348px; flex: 0 0 348px; height: 100%; overflow-y: auto;
    padding: 20px; background: #fff; border-right: 1px solid #e8e8ea;
  }
  #map { flex: 1; height: 100%; background: #e9e9eb; }
  h1 { font-size: 17px; margin: 0 0 12px; }
  .pills { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 18px; }
  .pill {
    font-size: 12px; padding: 3px 9px; border-radius: 999px;
    background: #f0f0f2; color: #52525b; border: 1px solid #e2e2e5;
    display: inline-flex; align-items: center; gap: 5px;
  }
  .pill i { width: 6px; height: 6px; border-radius: 50%; background: #b9b9c0; }
  .pill.ok { background: #e5fff2; border-color: #b4fdd9; color: #004d26; }
  .pill.ok i { background: #0fdc78; }
  .pill.bad { background: #fdecea; border-color: #f6c8c3; color: #8a241c; }
  .pill.bad i { background: #e8463a; }
  .card { border: 1px solid #e8e8ea; border-radius: 12px; padding: 14px; margin-bottom: 12px; }
  .card h2 { font-size: 12px; font-weight: 600; color: #52525b; margin: 0 0 10px; letter-spacing: .3px; }
  .row { display: flex; gap: 8px; margin-top: 8px; }
  .row > * { flex: 1; min-width: 0; }
  .field { display: flex; flex-direction: column; gap: 4px; }
  .field > span { font-size: 12px; line-height: 18px; color: #52525b; }
  .field em { font-style: normal; color: #9a9aa2; margin-left: 2px; }
  input, select, button {
    font: inherit; padding: 8px 10px; border-radius: 8px;
    border: 1px solid #dcdce0; background: #fff; color: #171717; outline: none;
  }
  input:focus, select:focus { border-color: #0fdc78; }
  button { cursor: pointer; background: #f7f7f8; transition: background .12s; white-space: nowrap; }
  button:hover { background: #efeff2; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  button.primary { background: #0fdc78; border-color: #0fdc78; color: #004d26; font-weight: 600; }
  button.primary:hover { background: #0ab861; }
  button.on { background: #004d26; border-color: #004d26; color: #e5fff2; }
  .cur { font: 13px/20px ui-monospace, "SF Mono", Menlo, monospace; word-break: break-all; }
  .cur.empty { color: #9a9aa2; }
  .hint { font-size: 12px; color: #8a8a93; margin-top: 8px; line-height: 1.5; }
  .path-list { margin: 10px 0 0; padding-left: 18px; max-height: 150px; overflow-y: auto; }
  .path-list:empty { display: none; }
  .path-list li { font: 12px/18px ui-monospace, "SF Mono", Menlo, monospace; color: #52525b; }
  .hist { list-style: none; margin: 0; padding: 0; max-height: 208px; overflow-y: auto; }
  .hist li {
    display: flex; align-items: baseline; gap: 8px; padding: 6px 8px;
    border-radius: 8px; cursor: pointer;
  }
  .hist li:hover { background: #f7f7f8; }
  .hist time { flex: 0 0 auto; font-size: 11px; line-height: 16px; color: #9a9aa2; }
  .hist span { font: 12px/16px ui-monospace, "SF Mono", Menlo, monospace; color: #171717; }
  .hist-empty { font-size: 12px; line-height: 18px; color: #9a9aa2; padding: 2px 8px; }
  .hist button.del {
    margin-left: auto; flex: 0 0 auto; border: 0; background: transparent;
    color: #c2c2c9; font-size: 16px; line-height: 1; padding: 0 2px;
  }
  .hist button.del:hover { color: #e8463a; background: transparent; }
  .results { list-style: none; margin: 8px 0 0; padding: 0; max-height: 210px; overflow-y: auto;
    border: 1px solid #ececef; border-radius: 8px; }
  .results[hidden] { display: none; }
  .results li { padding: 8px 10px; cursor: pointer; border-bottom: 1px solid #f2f2f4; }
  .results li:last-child { border-bottom: 0; }
  .results li:hover { background: #f7f7f8; }
  .results b { display: block; font-size: 13px; font-weight: 500; }
  .results small { display: block; font-size: 11px; line-height: 16px; color: #9a9aa2; margin-top: 2px; }
  pre {
    margin: 0 0 8px; padding: 10px; background: #f7f7f8; border-radius: 8px;
    font: 12px/18px ui-monospace, "SF Mono", Menlo, monospace;
    white-space: pre-wrap; word-break: break-all;
  }
  #toast {
    position: absolute; left: 50%; bottom: 24px; transform: translate(-50%, 12px);
    background: #171717; color: #fff; padding: 9px 16px; border-radius: 999px;
    font-size: 13px; opacity: 0; pointer-events: none; transition: all .18s; z-index: 999;
    max-width: 70vw;
  }
  #toast.show { opacity: 1; transform: translate(-50%, 0); }
  #toast.err { background: #c0392b; }
  .leaflet-container { font: inherit; }
  .pin {
    width: 14px; height: 14px; border-radius: 50%; background: #0fdc78;
    border: 2px solid #fff; box-shadow: 0 0 0 1px rgba(0,0,0,.25);
  }
  .num {
    width: 18px; height: 18px; border-radius: 50%; background: #171717; color: #fff;
    font-size: 11px; line-height: 18px; text-align: center;
  }
</style>
</head>
<body>
<aside id="side">
  <h1>iPhone 定位模拟</h1>
  <div class="pills">
    <span class="pill" id="p-dev"><i></i>设备</span>
    <span class="pill" id="p-sess"><i></i>会话</span>
    <span class="pill" id="p-play"><i></i>播放</span>
  </div>

  <section class="card">
    <h2>当前虚拟位置</h2>
    <div class="cur empty" id="cur">未设定</div>
  </section>

  <section class="card">
    <h2>目标坐标 · WGS-84</h2>
    <div class="row">
      <input id="q" placeholder="搜索地址，如「广州塔」">
      <button id="btn-search">搜索</button>
    </div>
    <ul class="results" id="results" hidden></ul>
    <div class="hint" id="key-hint" hidden>地址搜索需要高德 Web 服务 Key，免费申请：lbs.amap.com → 控制台 → 创建应用（服务平台选「Web服务」）。</div>
    <div class="row" id="key-row" hidden>
      <input id="amap-key" placeholder="粘贴高德 Key">
      <button id="btn-save-key">保存</button>
    </div>
    <div class="row">
      <label class="field">
        <span>纬度 <em>Lat · -90~90</em></span>
        <input id="lat" value="39.904030" placeholder="39.904030">
      </label>
      <label class="field">
        <span>经度 <em>Lng · -180~180</em></span>
        <input id="lng" value="116.407526" placeholder="116.407526">
      </label>
    </div>
    <div class="row">
      <button class="primary" id="btn-set">设定位置</button>
      <button id="btn-clear">恢复真实</button>
    </div>
    <div class="hint" id="hint">点击地图任意位置拾取坐标，会自动从高德 GCJ-02 转成 WGS-84。</div>
  </section>

  <section class="card">
    <h2>历史记录</h2>
    <ul class="hist" id="hist"></ul>
    <div class="hist-empty" id="hist-empty">还没有记录</div>
  </section>

  <section class="card">
    <h2>路径播放</h2>
    <div class="row">
      <button id="btn-mode">开启加点模式</button>
    </div>
    <ol class="path-list" id="path-list"></ol>
    <div class="row">
      <select id="speed">
        <option value="5">步行 5 km/h</option>
        <option value="15" selected>骑行 15 km/h</option>
        <option value="40">驾车 40 km/h</option>
        <option value="80">快速移动 80 km/h</option>
      </select>
      <button class="primary" id="btn-play" disabled>开始播放</button>
    </div>
    <div class="row">
      <button id="btn-reset">清空路径</button>
    </div>
  </section>

  <section class="card" id="setup" hidden>
    <h2 id="setup-title">排查</h2>
    <pre id="cmd"></pre>
    <div class="row">
      <button id="btn-copy">复制</button>
    </div>
  </section>
</aside>
<main id="map"></main>
<div id="toast"></div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
(function () {
  'use strict';

  // ---- 坐标系：高德瓦片是 GCJ-02，iPhone 需要 WGS-84，直接下发会偏几百米 ----
  var PI = Math.PI, AXIS = 6378245.0, EE = 0.00669342162296594323;

  function outOfChina(lat, lng) {
    return !(lng > 73.66 && lng < 135.05 && lat > 3.86 && lat < 53.55);
  }
  function transformLat(x, y) {
    var r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
    r += (20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0 / 3.0;
    r += (20.0 * Math.sin(y * PI) + 40.0 * Math.sin(y / 3.0 * PI)) * 2.0 / 3.0;
    r += (160.0 * Math.sin(y / 12.0 * PI) + 320 * Math.sin(y * PI / 30.0)) * 2.0 / 3.0;
    return r;
  }
  function transformLng(x, y) {
    var r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    r += (20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0 / 3.0;
    r += (20.0 * Math.sin(x * PI) + 40.0 * Math.sin(x / 3.0 * PI)) * 2.0 / 3.0;
    r += (150.0 * Math.sin(x / 12.0 * PI) + 300.0 * Math.sin(x / 30.0 * PI)) * 2.0 / 3.0;
    return r;
  }
  function delta(lat, lng) {
    var dLat = transformLat(lng - 105.0, lat - 35.0);
    var dLng = transformLng(lng - 105.0, lat - 35.0);
    var radLat = lat / 180.0 * PI;
    var magic = Math.sin(radLat);
    magic = 1 - EE * magic * magic;
    var sqrtMagic = Math.sqrt(magic);
    dLat = (dLat * 180.0) / ((AXIS * (1 - EE)) / (magic * sqrtMagic) * PI);
    dLng = (dLng * 180.0) / (AXIS / sqrtMagic * Math.cos(radLat) * PI);
    return [dLat, dLng];
  }
  function gcj2wgs(lat, lng) {
    if (outOfChina(lat, lng)) return [lat, lng];
    var d = delta(lat, lng);
    return [lat - d[0], lng - d[1]];
  }
  function wgs2gcj(lat, lng) {
    if (outOfChina(lat, lng)) return [lat, lng];
    var d = delta(lat, lng);
    return [lat + d[0], lng + d[1]];
  }
  function fmt(v) { return Number(v).toFixed(6); }

  var $ = function (id) { return document.getElementById(id); };
  var elLat = $('lat'), elLng = $('lng'), elCur = $('cur'), elHint = $('hint');
  var elList = $('path-list'), elSpeed = $('speed'), elSetup = $('setup');
  var btnSet = $('btn-set'), btnClear = $('btn-clear'), btnMode = $('btn-mode');
  var btnPlay = $('btn-play'), btnReset = $('btn-reset'), btnCopy = $('btn-copy');
  var pDev = $('p-dev'), pSess = $('p-sess'), pPlay = $('p-play'), elCmd = $('cmd');
  var setupTitle = $('setup-title');

  var map = L.map('map', { zoomControl: true, attributionControl: false })
    .setView([39.9087, 116.3975], 13);
  L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}', {
    subdomains: ['1', '2', '3', '4'], maxZoom: 18
  }).addTo(map);

  var pinLayer = null, pathLayer = null, mode = 'pin', points = [];

  function dotIcon(cls, label) {
    return L.divIcon({ className: '', html: '<div class="' + cls + '">' + (label || '') + '</div>',
      iconSize: [18, 18], iconAnchor: [9, 9] });
  }

  function drawPin(lat, lng) {
    if (pinLayer) pinLayer.remove();
    pinLayer = L.layerGroup([
      L.marker([lat, lng], { icon: dotIcon('pin'), interactive: false }),
      L.circle([lat, lng], { radius: 220, color: '#0fdc78', weight: 1, fillOpacity: 0.12, interactive: false })
    ]).addTo(map);
  }

  function redrawPath() {
    if (pathLayer) { pathLayer.remove(); pathLayer = null; }
    elList.innerHTML = '';
    if (!points.length) { btnPlay.disabled = true; return; }
    var layers = [];
    points.forEach(function (p, i) {
      layers.push(L.marker(p, { icon: dotIcon('num', String(i + 1)), interactive: false }));
      var li = document.createElement('li');
      li.textContent = fmt(p[0]) + ', ' + fmt(p[1]);
      elList.appendChild(li);
    });
    if (points.length > 1) {
      layers.push(L.polyline(points.map(function (p) { return wgs2gcj(p[0], p[1]); }),
        { color: '#0fdc78', weight: 3, opacity: 0.9 }));
    }
    pathLayer = L.layerGroup(layers).addTo(map);
    btnPlay.disabled = points.length < 2;
  }

  map.on('click', function (e) {
    var gcjLat = e.latlng.lat, gcjLng = e.latlng.lng;
    var wgs = gcj2wgs(gcjLat, gcjLng);
    if (mode === 'pin') {
      elLat.value = fmt(wgs[0]);
      elLng.value = fmt(wgs[1]);
      drawPin(gcjLat, gcjLng);
      elHint.textContent = '地图点 ' + fmt(gcjLat) + ', ' + fmt(gcjLng) + '（GCJ-02）→ 下发 ' +
        fmt(wgs[0]) + ', ' + fmt(wgs[1]) + '（WGS-84）';
    } else {
      points.push([wgs[0], wgs[1]]);
      redrawPath();
    }
  });

  function toast(msg, isErr) {
    var el = $('toast');
    el.textContent = msg;
    el.className = 'show' + (isErr ? ' err' : '');
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { el.className = ''; }, isErr ? 5000 : 2200);
  }

  async function post(url, body) {
    var res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
    var data = await res.json().catch(function () { return { ok: false, error: '响应解析失败' }; });
    if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
    return data;
  }

  btnSet.onclick = async function () {
    var lat = parseFloat(elLat.value), lng = parseFloat(elLng.value);
    if (!isFinite(lat) || !isFinite(lng)) { toast('请填写有效的经纬度', true); return; }
    btnSet.disabled = true;
    btnSet.textContent = '设定中…';
    try {
      await post('/api/location', { lat: lat, lng: lng });
      var gcj = wgs2gcj(lat, lng);
      drawPin(gcj[0], gcj[1]);
      map.setView([gcj[0], gcj[1]], Math.max(map.getZoom(), 14));
      toast('已接管定位：' + fmt(lat) + ', ' + fmt(lng));
      refresh();
      loadHistory();
    } catch (err) { toast(err.message, true); }
    btnSet.disabled = false;
    btnSet.textContent = '设定位置';
  };

  btnClear.onclick = async function () {
    btnClear.disabled = true;
    try {
      await post('/api/clear', {});
      if (pinLayer) { pinLayer.remove(); pinLayer = null; }
      toast('已结束模拟，设备回到真实定位');
      refresh();
    } catch (err) { toast(err.message, true); }
    btnClear.disabled = false;
  };

  btnMode.onclick = function () {
    mode = mode === 'pin' ? 'path' : 'pin';
    btnMode.classList.toggle('on', mode === 'path');
    btnMode.textContent = mode === 'path' ? '加点模式：开' : '开启加点模式';
  };

  btnReset.onclick = function () { points = []; redrawPath(); };

  btnPlay.onclick = async function () {
    btnPlay.disabled = true;
    btnPlay.textContent = '启动中…';
    try {
      var data = await post('/api/route', { points: points, speed: Number(elSpeed.value) });
      toast('开始播放：' + data.points + ' 个点，约 ' + Math.round(data.seconds) + ' 秒');
      refresh();
      loadHistory();
    } catch (err) {
      toast(err.message, true);
      btnPlay.disabled = points.length < 2;
    }
    btnPlay.textContent = '开始播放';
  };

  btnCopy.onclick = function () {
    var text = elCmd.textContent;
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text).then(function () { toast('已复制'); },
        function () { toast('复制失败，请手动选中', true); });
    } else { toast('请手动选中内容复制', true); }
  };

  function pill(el, cls, text) {
    el.className = 'pill ' + cls;
    el.innerHTML = '<i></i>' + text;
  }

  var elHist = $('hist'), elHistEmpty = $('hist-empty');

  function fmtTime(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    var p = function (n) { return n < 10 ? '0' + n : '' + n; };
    return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  async function loadHistory() {
    try {
      var data = await (await fetch('/api/history')).json();
      var items = data.items || [];
      elHist.innerHTML = '';
      elHistEmpty.hidden = items.length > 0;
      items.forEach(function (it) {
        var li = document.createElement('li');
        var t = document.createElement('time');
        t.textContent = fmtTime(it.at);
        var s = document.createElement('span');
        s.textContent = fmt(it.lat) + ', ' + fmt(it.lng);
        li.appendChild(t);
        li.appendChild(s);
        li.title = '点击填入坐标并在地图上定位';
        var del = document.createElement('button');
        del.className = 'del';
        del.type = 'button';
        del.textContent = '×';
        del.title = '删除这条记录';
        del.addEventListener('click', async function (ev) {
          ev.stopPropagation();
          try {
            await post('/api/history/delete', { id: it.id });
            loadHistory();
          } catch (err) { toast(err.message, true); }
        });
        li.appendChild(del);
        li.addEventListener('click', function () {
          elLat.value = fmt(it.lat);
          elLng.value = fmt(it.lng);
          var gcj = wgs2gcj(it.lat, it.lng);
          drawPin(gcj[0], gcj[1]);
          map.setView([gcj[0], gcj[1]], Math.max(map.getZoom(), 14));
          toast('已填入历史坐标，点「设定位置」下发');
        });
        elHist.appendChild(li);
      });
    } catch (err) { /* 历史加载失败不影响主流程 */ }
  }

  var elQ = $('q'), btnSearch = $('btn-search'), elResults = $('results');
  var elKeyHint = $('key-hint'), elKeyRow = $('key-row'), elKey = $('amap-key');
  var btnSaveKey = $('btn-save-key');

  function renderResults(items) {
    elResults.innerHTML = '';
    if (!items.length) { elResults.hidden = true; toast('没有找到匹配地址'); return; }
    items.forEach(function (it) {
      var li = document.createElement('li');
      var b = document.createElement('b');
      b.textContent = it.name || '';
      var s = document.createElement('small');
      s.textContent = it.addr || '';
      li.appendChild(b);
      li.appendChild(s);
      li.addEventListener('click', function () {
        var wgs = gcj2wgs(it.lat, it.lng);   // 高德返回的是 GCJ-02
        elLat.value = fmt(wgs[0]);
        elLng.value = fmt(wgs[1]);
        drawPin(it.lat, it.lng);
        map.setView([it.lat, it.lng], Math.max(map.getZoom(), 15));
        elResults.hidden = true;
        toast('已填入「' + (it.name || '') + '」，点「设定位置」下发');
      });
      elResults.appendChild(li);
    });
    elResults.hidden = false;
  }

  async function doSearch() {
    var q = elQ.value.trim();
    if (!q) { toast('请输入地址关键词', true); return; }
    btnSearch.disabled = true;
    try {
      var res = await fetch('/api/geocode?q=' + encodeURIComponent(q));
      var data = await res.json();
      if (!res.ok || !data.ok) {
        if (data.need_key) { elKeyHint.hidden = false; elKeyRow.hidden = false; }
        throw new Error(data.error || '搜索失败');
      }
      elKeyHint.hidden = true;
      elKeyRow.hidden = true;
      renderResults(data.items || []);
    } catch (err) { toast(err.message, true); }
    btnSearch.disabled = false;
  }

  btnSearch.addEventListener('click', doSearch);
  elQ.addEventListener('keydown', function (e) { if (e.key === 'Enter') doSearch(); });

  btnSaveKey.addEventListener('click', async function () {
    var key = elKey.value.trim();
    if (!key) { toast('请填写 Key', true); return; }
    try {
      await post('/api/config', { amap_key: key });
      elKeyHint.hidden = true;
      elKeyRow.hidden = true;
      elKey.value = '';
      toast('Key 已保存，可重新搜索');
    } catch (err) { toast(err.message, true); }
  });

  async function refresh() {
    try {
      var s = await (await fetch('/api/status')).json();

      if (!s.pmd3_ready) {
        pill(pDev, 'bad', '未装 pymobiledevice3');
      } else if (s.device) {
        var label = s.device.name;
        if (s.device.os) label += ' · iOS ' + s.device.os;
        pill(pDev, 'ok', label);
      } else {
        pill(pDev, 'bad', '未发现设备');
      }
      pill(pSess, s.session ? 'ok' : '', s.session ? '已接管定位' : '无会话');
      pill(pPlay, s.playing ? 'ok' : '', s.playing ? ('播放中 ' + Math.round(s.elapsed) + 's') : '空闲');

      if (s.simulated) {
        elCur.className = 'cur';
        elCur.textContent = fmt(s.simulated.lat) + ', ' + fmt(s.simulated.lng);
      } else {
        elCur.className = 'cur empty';
        elCur.textContent = '未设定（跟随真实定位）';
      }

      if (!s.pmd3_ready) {
        setupTitle.textContent = '先装运行环境';
        elCmd.textContent = s.install;
        elSetup.hidden = false;
      } else if (s.error) {
        setupTitle.textContent = '上次操作失败';
        elCmd.textContent = s.error;
        elSetup.hidden = false;
      } else if (!s.device) {
        setupTitle.textContent = '未发现设备';
        elCmd.textContent = '1. 数据线连接 iPhone，解锁后点「信任」\n2. 设置 → 隐私与安全性 → 开发者模式 → 打开并重启\n3. 或让 iPhone 与 Mac 处在同一 Wi-Fi 下';
        elSetup.hidden = false;
      } else {
        elSetup.hidden = true;
      }

      btnPlay.disabled = s.playing || points.length < 2;
    } catch (err) {
      pill(pDev, 'bad', '服务未连接');
      pill(pSess, 'bad', '服务未连接');
    }
  }

  loadHistory();
  refresh();
  setInterval(refresh, 2500);
})();
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="iPhone 定位模拟本地控制台")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()

    pmd3 = find_pmd3()
    device = discover(pmd3) if pmd3 else None
    print("iPhone 定位模拟控制台")
    print("  控制台地址 : http://127.0.0.1:%d" % args.port)
    print("  pymobiledevice3 : %s" % (pmd3 or "未安装"))
    print("  设备       : %s" % ("%s (iOS %s)" % (device["name"], device["os"] or "?") if device else "未发现"))
    if not pmd3:
        print("  提示：先执行 brew install python@3.12 并安装 pymobiledevice3")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open("http://127.0.0.1:%d" % args.port)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        stop_session()
        server.server_close()


if __name__ == "__main__":
    main()
