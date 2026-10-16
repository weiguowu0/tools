# -*- coding: utf-8 -*-
"""12306 车站代码表查询。"""
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data", "stations.json")

_cache = None


def load_stations():
    global _cache
    if _cache is None:
        with open(DATA, encoding="utf-8") as f:
            _cache = json.load(f)
    return _cache


def name_to_code(name):
    name = (name or "").strip()
    for s in load_stations():
        if s["name"] == name:
            return s["code"]
    return None


def code_to_name(code):
    code = (code or "").strip()
    for s in load_stations():
        if s["code"] == code:
            return s["name"]
    return code


def name_to_city(name):
    name = (name or "").strip()
    for s in load_stations():
        if s["name"] == name:
            return s["city"]
    return None


def code_to_city(code):
    code = (code or "").strip()
    for s in load_stations():
        if s["code"] == code:
            return s["city"]
    return None


def search(keyword):
    """按站名/拼音/简拼模糊搜索，返回候选列表（精确站名优先）。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return []
    result = []
    for s in load_stations():
        if (kw in s["name"] or kw in s["pinyin"] or kw in s["pc"] or kw in s["city"]):
            result.append({"name": s["name"], "code": s["code"], "city": s["city"]})
    result.sort(key=lambda x: (0 if x["name"] == keyword else 1, len(x["name"])))
    return result[:20]
