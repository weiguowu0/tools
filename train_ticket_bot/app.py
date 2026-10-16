# -*- coding: utf-8 -*-
"""12306 自动订票 - 本地 Web 界面与 API。"""
import json
import os
import threading
import webbrowser

from flask import Flask, jsonify, request, send_from_directory

import bot as botmod
import stations

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(botmod.CONFIG_DIR, "config.json")
PORT = 8765

app = Flask(__name__, static_folder="static", template_folder="templates")

DEFAULT_CONFIG = {
    "routes": [{"from": "北京", "to": "石家庄"}],
    "date": "2026-10-15",
    "train_numbers": [],
    "seat_priority": ["二等座", "硬卧", "硬座", "软卧"],
    "passenger": "",
    "ticket_type": "成人票",
    "start_time": "",
    "poll_interval": 0.8,
}


def normalize_routes(raw):
    """把各种 routes 形式规范成 [{'from':..., 'to':...}, ...]。"""
    out = []
    if isinstance(raw, str):
        for seg in raw.replace("，", ",").split(","):
            seg = seg.strip()
            if not seg:
                continue
            for sep in ("→", "->", "-", "至", "到"):
                if sep in seg:
                    f, t = seg.split(sep, 1)
                    out.append({"from": f.strip(), "to": t.strip()})
                    break
    elif isinstance(raw, list):
        for r in raw:
            if isinstance(r, dict):
                out.append({"from": (r.get("from") or "").strip(), "to": (r.get("to") or "").strip()})
            elif isinstance(r, (list, tuple)) and len(r) >= 2:
                out.append({"from": str(r[0]).strip(), "to": str(r[1]).strip()})
    return [r for r in out if r["from"] and r["to"]]


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                saved = json.load(f)
            # 迁移旧版单站配置 → routes
            if "routes" not in saved and saved.get("from_station"):
                saved["routes"] = [{"from": saved["from_station"], "to": saved.get("to_station", "")}]
            for k in cfg:
                if k in saved:
                    cfg[k] = saved[k]
            cfg["routes"] = normalize_routes(cfg.get("routes")) or DEFAULT_CONFIG["routes"]
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


@app.route("/")
def index():
    return send_from_directory(app.template_folder, "index.html")


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        cfg = load_config()
        data = request.get_json(force=True) or {}
        if "train_numbers" in data:
            raw = data["train_numbers"]
            if isinstance(raw, str):
                cfg["train_numbers"] = [t.strip().upper() for t in raw.replace("，", ",").replace(" ", ",").split(",") if t.strip()]
            else:
                cfg["train_numbers"] = [str(t).strip().upper() for t in raw if str(t).strip()]
        if "seat_priority" in data:
            raw = data["seat_priority"]
            if isinstance(raw, str):
                cfg["seat_priority"] = [s.strip() for s in raw.replace("，", ",").split(",") if s.strip()]
            else:
                cfg["seat_priority"] = [str(s).strip() for s in raw if str(s).strip()]
        if "routes" in data:
            cfg["routes"] = normalize_routes(data["routes"]) or cfg["routes"]
        for k in ("date", "passenger", "ticket_type", "start_time", "poll_interval"):
            if k in data:
                cfg[k] = data[k]
        save_config(cfg)
        return jsonify({"ok": True, "config": cfg})
    return jsonify(load_config())


@app.route("/api/state")
def api_state():
    state = botmod.get_state()
    return jsonify(state)


@app.route("/api/logs")
def api_logs():
    return jsonify(botmod.get_logs())


@app.route("/api/stations")
def api_stations():
    kw = request.args.get("kw", "")
    return jsonify(stations.search(kw))


@app.route("/api/login", methods=["POST"])
def api_login():
    botmod.get_manager().login_async()
    return jsonify({"ok": True})


@app.route("/api/passengers", methods=["POST"])
def api_passengers():
    try:
        res = botmod.get_manager().passengers(timeout=30)
        return jsonify(res)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/query", methods=["POST"])
def api_query():
    data = request.get_json(force=True) or {}
    date = (data.get("date") or "").strip()
    routes = normalize_routes(data.get("routes"))
    if not routes:
        return jsonify({"ok": False, "error": "未填写路线"})
    if not date:
        return jsonify({"ok": False, "error": "请填写日期"})
    codes = []
    for r in routes:
        fc = stations.name_to_code(r["from"])
        tc = stations.name_to_code(r["to"])
        if not fc:
            return jsonify({"ok": False, "error": "未找到出发站: " + r["from"]})
        if not tc:
            return jsonify({"ok": False, "error": "未找到到达站: " + r["to"]})
        codes.append((fc, tc))
    try:
        res = botmod.get_manager().query(codes, date, timeout=40)
        return jsonify(res)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(force=True) or {}
    cfg = load_config()
    # 允许前端直接传配置并保存
    if data:
        cfg.update(data)
        if "train_numbers" in data and isinstance(data["train_numbers"], str):
            cfg["train_numbers"] = [t.strip().upper() for t in data["train_numbers"].replace("，", ",").replace(" ", ",").split(",") if t.strip()]
        if "seat_priority" in data and isinstance(data["seat_priority"], str):
            cfg["seat_priority"] = [s.strip() for s in data["seat_priority"].replace("，", ",").split(",") if s.strip()]
        if "routes" in data:
            cfg["routes"] = normalize_routes(data["routes"]) or cfg["routes"]
        save_config(cfg)

    botmod.get_manager().run_async(cfg)
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    botmod.get_manager().stop()
    return jsonify({"ok": True})


def main():
    url = "http://127.0.0.1:{}".format(PORT)
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print("12306 自动订票已启动，界面地址: {}".format(url))
    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False)


if __name__ == "__main__":
    main()
