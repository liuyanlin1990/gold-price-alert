#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日金价查询 + 历史位置分析 + 多通道推送。

数据源：新浪财经贵金属行情接口（实时价 + 历史日线）。
推送方式：Server酱（微信推送，主通道） + QQ 邮箱（备份通道）。

规则：
- 每天第一次运行 → 发送一封「每日播报」（现价 + 历史分析 + 阈值对比）；
- 之后每次运行 → 只有当现价跌破阈值时，才立即加发一封「金价提醒」；
- alert_once_per_day=true 时，同一天最多只发一次跌破提醒。
- 敏感字段（smtp_auth_code / serverchan_sendkey）优先从同名环境变量读取，
  方便在 GitHub Actions / 云函数等云端环境通过 Secrets 注入。
"""

import json
import os
import re
import smtplib
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

# ── 路径常量 ──────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# ── 新浪接口 ──────────────────────────────────────────────────────────────
# 实时行情（上海黄金交易所 Au9999）
SINA_URL = "https://hq.sinajs.cn/list={symbol}"
SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0",
}
# 历史日线（tab 分隔 / GBK / 字段：日期 合约 开盘 最高 最低 收盘 …）
HISTORY_URL = (
    "http://vip.stock.finance.sina.com.cn/q/view/"
    "download_gold_history.php?breed={breed}&start={start}&end={end}"
)
HISTORY_HEADERS = {"User-Agent": "Mozilla/5.0"}


# ── 基础工具 ──────────────────────────────────────────────────────────────
def log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    line = "[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(os.path.join(LOG_DIR, "run.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def env_or_cfg(cfg, key, env_key=None, default=""):
    """优先从环境变量取值，其次从 config.json，最后 default。"""
    env = os.environ.get(env_key or key)
    if env:
        return env.strip()
    val = cfg.get(key, default)
    return val.strip() if isinstance(val, str) else val


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ── 实时行情 ──────────────────────────────────────────────────────────────
def fetch_price(symbol):
    """返回 {symbol, name, price, prev, date, time}"""
    req = urllib.request.Request(
        SINA_URL.format(symbol=symbol), headers=SINA_HEADERS
    )
    raw = urllib.request.urlopen(req, timeout=15).read().decode(
        "gb2312", errors="ignore"
    )
    m = re.search(r'="([^"]*)"', raw)
    if not m or not m.group(1):
        raise RuntimeError("行情接口返回内容无法解析: {!r}".format(raw))
    parts = m.group(1).split(",")
    if len(parts) < 14:
        raise RuntimeError("行情字段数量异常: {}".format(parts))
    return {
        "symbol": symbol,
        # 新浪贵金属字段: 0=最新价 6=时间 7=昨收 12=日期 13=名称
        "name": parts[13] or symbol,
        "price": float(parts[0]),
        "time": parts[6],
        "prev": float(parts[7]) if parts[7] else float(parts[0]),
        "date": parts[12],
    }


# ── 历史日线 ──────────────────────────────────────────────────────────────
def fetch_history(days, breed="AU9999"):
    """从新浪贵金属接口抓取近 days 天的日线，返回 list of dict：
    [{date, open, high, low, close}, ...]  按日期倒序（最新在前）。
    若抓取失败则返回空列表。
    """
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y-%m-%d")
    url = HISTORY_URL.format(breed=breed, start=start, end=end)
    try:
        req = urllib.request.Request(url, headers=HISTORY_HEADERS)
        raw = urllib.request.urlopen(req, timeout=15).read()
        text = raw.decode("gbk", errors="ignore")
    except Exception as e:  # noqa: BLE001
        log("历史日线抓取失败: {}: {}".format(type(e).__name__, e))
        return []

    lines = text.strip().splitlines()
    if len(lines) < 2:
        return []

    rows = []
    for line in lines[1:]:  # 跳过表头
        line = line.strip()
        if not line:
            continue
        fields = re.split(r"\s{2,}|\t+", line)
        # 新浪返回用多个空格分隔
        if len(fields) < 6:
            continue
        try:
            rows.append({
                "date": fields[0],
                "open": float(fields[2]),
                "high": float(fields[3]),
                "low": float(fields[4]),
                "close": float(fields[5]),
            })
        except (ValueError, IndexError):
            continue

    # 取最近 days 个交易日（已按日期倒序）
    return rows[:days]


# ── 分析 & 建议 ───────────────────────────────────────────────────────────
def analyze_price(history, current_price, days_label="60"):
    """基于历史日线 + 当前价计算百分位和简易建议。"""
    result = {
        "days_label": days_label,
        "high": None,
        "low": None,
        "percentile": None,
        "advice": "暂无历史数据，建议观望",
    }
    if not history:
        return result

    closes = [r["close"] for r in history]
    highs = [r["high"] for r in history]
    lows = [r["low"] for r in history]

    high = max(highs)
    low = min(lows)
    result["high"] = high
    result["low"] = low

    span = high - low
    if span > 0:
        percentile = (current_price - low) / span * 100
    else:
        percentile = 50.0
    result["percentile"] = percentile

    # 简易规则建议
    if percentile <= 20:
        result["advice"] = "✅ 历史低位（{:.0f}%），可考虑分批买入".format(percentile)
    elif percentile <= 40:
        result["advice"] = "🟡 偏低位（{:.0f}%），可以逐步建仓".format(percentile)
    elif percentile <= 60:
        result["advice"] = "➖ 中位区间（{:.0f}%），观望为主".format(percentile)
    elif percentile <= 80:
        result["advice"] = "🟠 偏高位（{:.0f}%），谨慎追高".format(percentile)
    else:
        result["advice"] = "🔴 历史高位（{:.0f}%），注意回调风险".format(percentile)

    # 额外补充：距 N 日最低还有多少空间
    pct_vs_low = (current_price - low) / low * 100
    pct_vs_high = (high - current_price) / high * 100
    result["pct_vs_low"] = pct_vs_low
    result["pct_vs_high"] = pct_vs_high
    return result


# ── 推送：Server酱（微信） ───────────────────────────────────────────────
def push_serverchan(sendkey, title, desp):
    """Server酱 微信推送。返回 True 成功 / False 跳过或失败。"""
    if not sendkey:
        log("未配置 serverchan_sendkey，跳过 Server酱 推送")
        return False
    # Server酱 免费版 title <= 32 / desp <= 32KB
    if len(title) > 32:
        title = title[:31] + "…"
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    url = "https://sctapi.ftqq.com/{}.send".format(sendkey)
    try:
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        resp = urllib.request.urlopen(req, timeout=15).read().decode("utf-8")
        obj = json.loads(resp)
        if obj.get("code") == 0:
            log("Server酱推送成功: {}".format(obj.get("message", "")))
            return True
        else:
            log("Server酱推送失败: {}".format(resp))
            return False
    except Exception as e:  # noqa: BLE001
        log("Server酱推送异常: {}: {}".format(type(e).__name__, e))
        return False


# ── 推送：邮件（备份） ────────────────────────────────────────────────────
def push_mail(cfg, subject, html):
    host = cfg.get("smtp_host", "smtp.qq.com")
    port = int(cfg.get("smtp_port", 465))
    user = env_or_cfg(cfg, "smtp_user")
    auth_code = env_or_cfg(cfg, "smtp_auth_code", "SMTP_AUTH_CODE")
    to = env_or_cfg(cfg, "mail_to") or user
    if not user or not auth_code:
        log("未配置 smtp_user / smtp_auth_code，跳过邮件发送")
        return False

    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("金价助手", "utf-8")), user))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)

    try:
        with smtplib.SMTP_SSL(host, port, timeout=20) as server:
            server.login(user, auth_code)
            server.sendmail(user, [to], msg.as_string())
        log("邮件已发送至 {}".format(to))
        return True
    except Exception as e:  # noqa: BLE001
        log("邮件发送失败: {}: {}".format(type(e).__name__, e))
        return False


# ── 消息体构建 ────────────────────────────────────────────────────────────
def build_title(info, threshold, triggered):
    price = info["price"]
    if triggered:
        return "【金价提醒】{:.2f} 低于阈值 {:.2f}".format(price, threshold)
    return "【每日金价】{:.2f} 元/克".format(price)


def build_markdown(info, analysis, threshold, triggered):
    """Server酱/Markdown 消息体（微信推送用）。"""
    price = info["price"]
    prev = info["prev"]
    change = price - prev
    pct = (change / prev * 100) if prev else 0.0

    lines = []
    if triggered:
        lines.append("### 🔔 金价提醒")
        lines.append("**现价 {:.2f} 元/克** 已低于你的阈值 {:.2f} 元/克！".format(price, threshold))
    else:
        lines.append("### 📊 每日金价播报")
        lines.append("**现价 {:.2f} 元/克**".format(price))

    lines.append("")
    lines.append("**涨跌**: {:+.2f} ({:+.2f}%)".format(change, pct))
    lines.append("**阈值**: {:.2f} 元/克".format(threshold))
    lines.append("")

    # 历史分析
    if analysis["high"] and analysis["low"]:
        lines.append("---")
        lines.append("### 📈 近 {} 日历史分析".format(analysis["days_label"]))
        lines.append("- 区间最高: **{:.2f}**".format(analysis["high"]))
        lines.append("- 区间最低: **{:.2f}**".format(analysis["low"]))
        lines.append("- 当前位置: **{:.0f}%** (距最低 {:.1f}% / 距最高 {:.1f}%)".format(
            analysis["percentile"],
            analysis.get("pct_vs_low", 0),
            analysis.get("pct_vs_high", 0),
        ))
        lines.append("- **建议**: {}".format(analysis["advice"]))
    else:
        lines.append("### 📈 历史分析")
        lines.append(analysis["advice"])

    lines.append("")
    lines.append("_数据时间 {} {} ｜ 来源：新浪财经_".format(info["date"], info["time"]))
    return "\n".join(lines)


def build_html(info, analysis, threshold, triggered):
    """HTML 邮件消息体。"""
    price = info["price"]
    prev = info["prev"]
    change = price - prev
    pct = (change / prev * 100) if prev else 0.0
    if change > 0:
        chg_txt = "上涨 +{:.2f} ({:+.2f}%)".format(change, pct)
    elif change < 0:
        chg_txt = "下跌 {:.2f} ({:+.2f}%)".format(change, pct)
    else:
        chg_txt = "持平"

    if triggered:
        accent = "#d93025"
        title = "金价低于阈值提醒"
        head = '<b style="color:#d93025;font-size:18px">🔔 金价提醒</b>'
    else:
        accent = "#1a7f37"
        title = "每日金价播报"
        head = '<b style="color:#1a7f37;font-size:18px">📊 每日金价播报</b>'

    # 历史分析区
    hist_block = ""
    if analysis["high"] and analysis["low"]:
        hist_block = """
        <hr style="border:none;border-top:1px solid #eee;margin:14px 0">
        <p style="margin:0">
          <b>📈 近 {days} 日历史分析</b><br>
          区间最高 <b>{high:.2f}</b> ｜ 区间最低 <b>{low:.2f}</b><br>
          当前位置 <b style="color:{accent}">{pct:.0f}%</b>
          (距最低 +{vs_low:.1f}% / 距最高 -{vs_high:.1f}%)<br>
          💡 <b>{advice}</b>
        </p>
        """.format(
            days=analysis["days_label"],
            high=analysis["high"],
            low=analysis["low"],
            pct=analysis["percentile"],
            vs_low=analysis.get("pct_vs_low", 0),
            vs_high=analysis.get("pct_vs_high", 0),
            advice=analysis["advice"],
            accent=accent,
        )

    html = f"""<div style="font-family:Arial,'Microsoft YaHei',sans-serif;max-width:520px">
  {head}
  <h2 style="color:{accent};margin:4px 0 12px 0">{title}</h2>
  <p style="font-size:15px;line-height:1.9;margin:0">
    现价：<b style="color:{accent};font-size:20px">{price:.2f} 元/克</b><br>
    较昨收：{chg_txt}<br>
    你的阈值：{threshold:.2f} 元/克
  </p>
  {hist_block}
  <hr style="border:none;border-top:1px solid #eee;margin:14px 0">
  <p style="color:#888;font-size:12px;margin:0">数据时间 {info['date']} {info['time']} ｜ 来源：新浪财经</p>
</div>"""
    return title, html


# ── 主流程 ───────────────────────────────────────────────────────────────
def main():
    cfg = load_config()
    threshold = float(cfg.get("threshold", 0))
    alert_once_per_day = bool(cfg.get("alert_once_per_day", True))
    history_days = int(cfg.get("history_days", 60))
    symbol = cfg.get("symbol", "gds_AU9999")
    sendkey = env_or_cfg(cfg, "serverchan_sendkey", "SERVERCHAN_SENDKEY")

    try:
        today = datetime.now().strftime("%Y-%m-%d")
        info = fetch_price(symbol)
        triggered = info["price"] < threshold
        log(
            "{} 现价 {:.2f} 元/克 | 阈值 {:.2f} | 触发={}".format(
                info["name"], info["price"], threshold, triggered
            )
        )

        # 历史分析
        history = fetch_history(history_days)
        analysis = analyze_price(history, info["price"], str(history_days))
        if analysis["percentile"] is not None:
            log(
                "近 {}日 高 {:.2f} / 低 {:.2f} / 当前位置 {:.0f}%".format(
                    analysis["days_label"],
                    analysis["high"],
                    analysis["low"],
                    analysis["percentile"],
                )
            )
        log("建议: {}".format(analysis["advice"]))

        state = load_state()
        sent_any = False

        # 1) 跌破阈值 → 立即提醒
        if triggered and (
            not alert_once_per_day or state.get("last_alert_date") != today
        ):
            title = build_title(info, threshold, True)
            md = build_markdown(info, analysis, threshold, True)
            _, html = build_html(info, analysis, threshold, True)
            push_serverchan(sendkey, title, md)
            push_mail(cfg, title, html)
            state["last_alert_date"] = today
            sent_any = True
            log("跌破阈值提醒已推送")

        # 2) 每天第一次运行 → 每日播报（若本次已发提醒则不重复发）
        if state.get("last_digest_date") != today:
            if not sent_any:
                title = build_title(info, threshold, False)
                md = build_markdown(info, analysis, threshold, False)
                _, html = build_html(info, analysis, threshold, False)
                push_serverchan(sendkey, title, md)
                push_mail(cfg, title, html)
                sent_any = True
                log("每日播报已推送")
            state["last_digest_date"] = today

        save_state(state)
        if not sent_any:
            log("本次无需发送（今日已播报，且现价未跌破阈值）")
        return 0
    except Exception as e:  # noqa: BLE001
        log("执行失败: {}: {}".format(type(e).__name__, e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
