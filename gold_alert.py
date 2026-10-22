#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""金价持续监测 + 历史位置分析 + 趋势判断 + 多通道推送。

数据源：新浪财经贵金属行情接口（实时价 + 历史日线）。
推送方式：Server酱（微信推送，主通道） + QQ 邮箱（备份通道）。

规则：
- GitHub Actions 每 15 分钟触发一次；每次 Run 内连续监测 monitor_seconds 秒，
  每 monitor_interval 秒抓一次价（采样密度≈1 分钟/次）。
- 每天第一条运行 → 发送一封「每日播报」。
- 跌破阈值 → 发「金价提醒」，alert_cooldown_minutes 内不重复。
- 高于阈值但差距小于 near_threshold_margin → 每天发一次「接近阈值」预警。
- 每次消息都包含：现价 + 历史百分位 + 技术指标趋势判断。
- 敏感字段优先从同名环境变量读取，方便云端 Secrets 注入。
"""

import json
import os
import re
import smtplib
import sys
import time
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
SINA_URL = "https://hq.sinajs.cn/list={symbol}"
SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0",
}
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
        except Exception:
            pass
    return {}


def save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def parse_ts(ts_str):
    """把 state.json 里的 ISO 时间字符串转回 datetime。"""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None


# ── 实时行情 ──────────────────────────────────────────────────────────────
def fetch_price(symbol):
    req = urllib.request.Request(
        SINA_URL.format(symbol=symbol), headers=SINA_HEADERS
    )
    raw = urllib.request.urlopen(req, timeout=15).read().decode(
        "gb2312", errors="ignore"
    )
    m = re.search(r'="([^"]*)"', raw)
    if not m or not m.group(1):
        raise RuntimeError("行情接口返回内容无法解析")
    parts = m.group(1).split(",")
    if len(parts) < 14:
        raise RuntimeError("行情字段数量异常")
    return {
        "symbol": symbol,
        "name": parts[13] or symbol,
        "price": float(parts[0]),
        "time": parts[6],
        "prev": float(parts[7]) if parts[7] else float(parts[0]),
        "date": parts[12],
    }


# ── 历史日线 ──────────────────────────────────────────────────────────────
def fetch_history(days, breed="AU9999"):
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y-%m-%d")
    url = HISTORY_URL.format(breed=breed, start=start, end=end)
    try:
        req = urllib.request.Request(url, headers=HISTORY_HEADERS)
        raw = urllib.request.urlopen(req, timeout=15).read()
        text = raw.decode("gbk", errors="ignore")
    except Exception as e:
        log("历史日线抓取失败: {}: {}".format(type(e).__name__, e))
        return []

    lines = text.strip().splitlines()
    if len(lines) < 2:
        return []

    rows = []
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        fields = re.split(r"\s{2,}|\t+", line)
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
    return rows[:days]  # 已按日期倒序


# ── 历史位置分析 ───────────────────────────────────────────────────────────
def analyze_price(history, current_price, days_label="60"):
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
    percentile = (current_price - low) / span * 100 if span > 0 else 50.0
    result["percentile"] = percentile
    result["pct_vs_low"] = (current_price - low) / low * 100
    result["pct_vs_high"] = (high - current_price) / high * 100

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
    return result


# ── 技术指标 & 趋势判断 ────────────────────────────────────────────────────
def calc_ma(closes, n):
    """计算 N 日均线（简单平均）。closes 必须是时间正序（最早在前）。"""
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def calc_rsi(closes, period=14):
    """相对强弱指标 RSI(period)，返回 0~100。closes 时间正序。"""
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(-diff)
    # Wilder's 平均法
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def linear_slope(closes):
    """收盘价线性回归斜率。返回每天的平均涨跌额。closes 时间正序。"""
    n = len(closes)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(closes) / n
    num = sum((i - mean_x) * (closes[i] - mean_y) for i in range(n))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return num / den if den else 0.0


def analyze_trend(history):
    """综合 MA 交叉 / RSI / 斜率 给出趋势判断。"""
    trend = {
        "ma5": None, "ma10": None, "ma20": None,
        "ma_signal": "",
        "rsi": None, "rsi_signal": "",
        "slope_10": 0.0, "slope_10_signal": "",
        "summary": "",
    }
    if not history or len(history) < 20:
        trend["summary"] = "历史数据不足，无法判断趋势"
        return trend

    # history 是倒序，翻转得到时间正序
    closes = [r["close"] for r in reversed(history)]

    trend["ma5"] = calc_ma(closes, 5)
    trend["ma10"] = calc_ma(closes, 10)
    trend["ma20"] = calc_ma(closes, 20)

    # MA 交叉信号
    if trend["ma5"] and trend["ma10"]:
        if trend["ma5"] > trend["ma10"] * 1.005:
            trend["ma_signal"] = "🟢 MA5 > MA10，短期多头排列"
        elif trend["ma5"] < trend["ma10"] * 0.995:
            trend["ma_signal"] = "🔴 MA5 < MA10，短期空头排列"
        else:
            trend["ma_signal"] = "⚪ MA5 ≈ MA10，方向不明朗"

    # RSI
    trend["rsi"] = calc_rsi(closes, 14)
    if trend["rsi"] is not None:
        if trend["rsi"] >= 70:
            trend["rsi_signal"] = "⚠️ RSI={:.0f} 超买，可能回调".format(trend["rsi"])
        elif trend["rsi"] <= 30:
            trend["rsi_signal"] = "💡 RSI={:.0f} 超卖，可能反弹".format(trend["rsi"])
        else:
            trend["rsi_signal"] = "➡️ RSI={:.0f} 中性".format(trend["rsi"])

    # 近 10 日斜率
    trend["slope_10"] = linear_slope(closes[-10:])
    if trend["slope_10"] > 1.0:
        trend["slope_10_signal"] = "📈 近10日呈上涨趋势（日均 +{:.2f}）".format(trend["slope_10"])
    elif trend["slope_10"] < -1.0:
        trend["slope_10_signal"] = "📉 近10日呈下跌趋势（日均 {:.2f}）".format(trend["slope_10"])
    else:
        trend["slope_10_signal"] = "➖ 近10日横盘震荡"

    # 综合
    signals = []
    if trend["ma_signal"]:
        signals.append(trend["ma_signal"])
    if trend["slope_10_signal"]:
        signals.append(trend["slope_10_signal"])
    trend["summary"] = " ｜ ".join(signals)
    return trend


# ── 推送：Server酱（微信） ───────────────────────────────────────────────
def push_serverchan(sendkey, title, desp):
    if not sendkey:
        log("未配置 serverchan_sendkey，跳过 Server酱 推送")
        return False
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
            log("Server酱推送成功")
            return True
        else:
            log("Server酱推送失败: {}".format(resp))
            return False
    except Exception as e:
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
    except Exception as e:
        log("邮件发送失败: {}: {}".format(type(e).__name__, e))
        return False


# ── 消息体构建 ────────────────────────────────────────────────────────────
DISCLAIMER = "_⚠️ 以上分析基于历史数据和技术指标，仅供参考，不构成投资建议。_"


def build_title(info, threshold, kind):
    price = info["price"]
    if kind == "alert":
        return "【金价提醒】{:.2f} 低于阈值 {:.2f}".format(price, threshold)
    if kind == "near":
        return "【接近阈值】{:.2f} 元/克".format(price)
    return "【金价播报】{:.2f} 元/克".format(price)


def build_markdown(info, analysis, trend, threshold, kind):
    price = info["price"]
    prev = info["prev"]
    change = price - prev
    pct = (change / prev * 100) if prev else 0.0

    lines = []
    if kind == "alert":
        lines.append("### 🔔 金价提醒")
        lines.append("**现价 {:.2f} 元/克** 已低于阈值 {:.2f}！".format(price, threshold))
    elif kind == "near":
        lines.append("### ⚠️ 接近阈值")
        lines.append("**现价 {:.2f} 元/克**，距阈值 {:.2f} 仅差 {:.2f} 元".format(
            price, threshold, price - threshold))
    else:
        lines.append("### 📊 金价播报")
        lines.append("**现价 {:.2f} 元/克**".format(price))

    lines.append("**涨跌**: {:+.2f} ({:+.2f}%)".format(change, pct))
    lines.append("**阈值**: {:.2f} 元/克".format(threshold))
    if info.get("icbc") is not None:
        lines.append("**工行积存金参考**: {:.2f} 元/克".format(info["icbc"]))

    # 历史位置
    if analysis["high"] and analysis["low"]:
        lines.append("")
        lines.append("---")
        lines.append("### 📍 历史位置（近 {} 日）".format(analysis["days_label"]))
        lines.append("- 区间最高 **{:.2f}** ｜ 最低 **{:.2f}**".format(analysis["high"], analysis["low"]))
        lines.append("- 当前位置 **{:.0f}%** (距最低 {:+.1f}% / 距最高 {:+.1f}%)".format(
            analysis["percentile"], analysis.get("pct_vs_low", 0), analysis.get("pct_vs_high", 0)))
        lines.append("- 💡 **{}**".format(analysis["advice"]))

    # 趋势判断
    lines.append("")
    lines.append("---")
    lines.append("### 📈 趋势判断（技术指标）")
    if trend.get("ma5"):
        lines.append("- MA5={:.2f}  MA10={:.2f}  MA20={:.2f}".format(
            trend["ma5"], trend["ma10"], trend["ma20"]))
    if trend.get("ma_signal"):
        lines.append("- {}".format(trend["ma_signal"]))
    if trend.get("rsi_signal"):
        lines.append("- {}".format(trend["rsi_signal"]))
    if trend.get("slope_10_signal"):
        lines.append("- {}".format(trend["slope_10_signal"]))

    lines.append("")
    lines.append(DISCLAIMER)
    lines.append("")
    lines.append("_数据时间 {} {} ｜ 来源：新浪财经_".format(info["date"], info["time"]))
    return "\n".join(lines)


def build_html(info, analysis, trend, threshold, kind):
    price = info["price"]
    prev = info["prev"]
    change = price - prev
    pct = (change / prev * 100) if prev else 0.0
    chg_txt = "上涨 +{:.2f} ({:+.2f}%)".format(change, pct) if change > 0 else \
        ("下跌 {:.2f} ({:+.2f}%)".format(change, pct) if change < 0 else "持平")
    icbc_txt = ("{:.2f} 元/克".format(info["icbc"])
                if info.get("icbc") is not None else "—")

    if kind == "alert":
        accent = "#d93025"
        title_text = "金价低于阈值提醒"
        head = '<b style="color:#d93025;font-size:18px">🔔 金价提醒</b>'
    elif kind == "near":
        accent = "#b26a00"
        title_text = "金价接近阈值提醒"
        head = '<b style="color:#b26a00;font-size:18px">⚠️ 接近阈值</b>'
    else:
        accent = "#1a7f37"
        title_text = "金价播报"
        head = '<b style="color:#1a7f37;font-size:18px">📊 金价播报</b>'

    # 历史位置区
    hist_block = ""
    if analysis["high"] and analysis["low"]:
        hist_block = """
        <hr style="border:none;border-top:1px solid #eee;margin:14px 0">
        <p style="margin:0;font-size:14px;line-height:1.9">
          <b>📍 历史位置（近 {days} 日）</b><br>
          区间最高 <b>{high:.2f}</b> ｜ 最低 <b>{low:.2f}</b><br>
          当前位置 <b style="color:{accent}">{pct:.0f}%</b>
          (距最低 {vs_low:+.1f}% / 距最高 {vs_high:+.1f}%)<br>
          💡 <b>{advice}</b>
        </p>""".format(
            days=analysis["days_label"], high=analysis["high"], low=analysis["low"],
            pct=analysis["percentile"], vs_low=analysis.get("pct_vs_low", 0),
            vs_high=analysis.get("pct_vs_high", 0), advice=analysis["advice"], accent=accent)

    # 趋势区
    trend_block = ""
    if trend.get("summary"):
        rsi_txt = trend.get("rsi_signal", "")
        ma_txt = trend.get("ma_signal", "")
        slope_txt = trend.get("slope_10_signal", "")
        ma_vals = ""
        if trend.get("ma5"):
            ma_vals = "MA5={:.2f}  MA10={:.2f}  MA20={:.2f}".format(
                trend["ma5"], trend["ma10"], trend["ma20"])
        trend_block = """
        <hr style="border:none;border-top:1px solid #eee;margin:14px 0">
        <p style="margin:0;font-size:14px;line-height:1.9">
          <b>📈 趋势判断（技术指标）</b><br>
          {ma_vals}<br>
          {ma_txt}<br>
          {slope_txt}<br>
          {rsi_txt}
        </p>
        <p style="color:#999;font-size:11px;margin:6px 0 0 0">⚠️ 以上分析基于历史数据和技术指标，仅供参考，不构成投资建议。</p>""".format(
            ma_vals=ma_vals, ma_txt=ma_txt, slope_txt=slope_txt, rsi_txt=rsi_txt)

    html = f"""<div style="font-family:Arial,'Microsoft YaHei',sans-serif;max-width:560px">
  {head}
  <h2 style="color:{accent};margin:4px 0 12px 0">{title_text}</h2>
  <p style="font-size:15px;line-height:1.9;margin:0">
    现价：<b style="color:{accent};font-size:20px">{price:.2f} 元/克</b><br>
    较昨收：{chg_txt}<br>
    你的阈值：{threshold:.2f} 元/克<br>
    工行积存金参考：{icbc_txt}
  </p>
  {hist_block}
  {trend_block}
  <hr style="border:none;border-top:1px solid #eee;margin:14px 0">
  <p style="color:#888;font-size:12px;margin:0">数据时间 {info['date']} {info['time']} ｜ 来源：新浪财经</p>
</div>"""
    return title_text, html


# ── 单次检查 ──────────────────────────────────────────────────────────────
def send_message(cfg, info, kind, threshold, history_days, sendkey):
    """抓历史数据、生成分析并推送（Server酱 + 邮件）。"""
    history = fetch_history(history_days)
    analysis = analyze_price(history, info["price"], str(history_days))
    trend = analyze_trend(history)

    if analysis["percentile"] is not None:
        log("近 {}日 高 {:.2f} / 低 {:.2f} / 位置 {:.0f}%".format(
            analysis["days_label"], analysis["high"], analysis["low"], analysis["percentile"]))
    if trend["summary"]:
        log("趋势: {}".format(trend["summary"]))
    if trend.get("rsi") is not None:
        log("RSI(14) = {:.1f}".format(trend["rsi"]))

    # 工行积存金参考价（= 沪金99 + icbc_spread）
    spread = float(cfg.get("icbc_spread", 0) or 0)
    if spread:
        info["icbc"] = info["price"] + spread

    title = build_title(info, threshold, kind)
    md = build_markdown(info, analysis, trend, threshold, kind)
    _, html = build_html(info, analysis, trend, threshold, kind)
    push_serverchan(sendkey, title, md)
    push_mail(cfg, title, html)
    log("[{}] 推送完成: {}".format(kind, title))


def check_once(cfg, state):
    """抓一次价格并判断是否需要推送，state 会被就地更新。"""
    threshold = float(cfg.get("threshold", 0))
    cooldown_minutes = int(cfg.get("alert_cooldown_minutes", 60))
    history_days = int(cfg.get("history_days", 60))
    near_margin = float(cfg.get("near_threshold_margin", 2.0))
    symbol = cfg.get("symbol", "gds_AU9999")
    sendkey = env_or_cfg(cfg, "serverchan_sendkey", "SERVERCHAN_SENDKEY")

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    info = fetch_price(symbol)
    price = info["price"]
    below = price < threshold
    near = (not below) and (price - threshold <= near_margin)
    log("{} 现价 {:.2f} 元/克 | 阈值 {:.2f} | 跌破={} | 接近={}".format(
        info["name"], price, threshold, below, near))

    # 休市判断：报价时间超过 20 分钟未更新，说明金交所已收盘，避免用冻结价反复报警
    try:
        quote_dt = datetime.strptime(
            info["date"] + " " + info["time"], "%Y-%m-%d %H:%M:%S")
        if (now - quote_dt).total_seconds() > 20 * 60:
            log("行情未更新（报价 {} {}，休市中），跳过本次判断".format(
                info["date"], info["time"]))
            return False
    except (ValueError, KeyError):
        pass

    sent_any = False

    # 1) 跌破阈值 → 冷却判断
    if below:
        last_alert = parse_ts(state.get("last_alert_ts"))
        cooldown_ok = (last_alert is None or
                       (now - last_alert).total_seconds() >= cooldown_minutes * 60)
        if cooldown_ok:
            send_message(cfg, info, "alert", threshold, history_days, sendkey)
            state["last_alert_ts"] = now.isoformat()
            state["last_alert_date"] = today
            sent_any = True
            log("跌破阈值提醒已推送（冷却 {} 分钟）".format(cooldown_minutes))
        else:
            remaining = cooldown_minutes * 60 - int((now - last_alert).total_seconds())
            log("跌破阈值但冷却中，剩余 {} 秒".format(max(remaining, 0)))

    # 2) 每天第一条：每日播报（本次已发提醒则不重复）
    if state.get("last_digest_date") != today:
        if not sent_any:
            send_message(cfg, info, "digest", threshold, history_days, sendkey)
            sent_any = True
            log("每日播报已推送")
        state["last_digest_date"] = today

    # 3) 接近阈值（每天最多提醒一次）
    elif near and state.get("last_near_date") != today:
        send_message(cfg, info, "near", threshold, history_days, sendkey)
        state["last_near_date"] = today
        sent_any = True

    if not sent_any:
        log("本次无需发送（今日已播报，{}）".format(
            "跌破但冷却中" if below else "未跌破阈值"))
    return below and sent_any


# ── 主流程 ───────────────────────────────────────────────────────────────
def monitor(cfg, seconds, interval):
    """在同一个 Run 内循环监测，显著提高采样密度。"""
    state = load_state()
    deadline = time.monotonic() + seconds
    log("开始循环监测：窗口 {} 秒，间隔 {} 秒".format(seconds, interval))
    while True:
        try:
            check_once(cfg, state)
            save_state(state)
        except Exception as e:
            log("本次检查失败: {}: {}".format(type(e).__name__, e))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    log("循环监测窗口结束")
    return 0


def main():
    cfg = load_config()
    loop_seconds = int(os.environ.get("MONITOR_SECONDS") or cfg.get("monitor_seconds", 0))
    interval = int(os.environ.get("MONITOR_INTERVAL") or cfg.get("monitor_interval", 60))

    if loop_seconds > 0:
        return monitor(cfg, loop_seconds, max(interval, 10))

    state = load_state()
    try:
        check_once(cfg, state)
        save_state(state)
        return 0
    except Exception as e:
        log("执行失败: {}: {}".format(type(e).__name__, e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
