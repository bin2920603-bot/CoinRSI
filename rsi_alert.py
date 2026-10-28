import os, json, time, requests

OKX = "https://www.okx.com"
TOP_N = 100            # 거래대금 상위 몇 개를 볼지
LOW = 30               # RSI 바닥 기준
RSI_SIGNAL = 6         # 알림 기준 RSI (차트의 RSI6)
RSI_SHOW = (6, 12, 24) # 알림에 같이 보여 줄 RSI
EMAS = (20, 50)        # 가격이 위로 뚫는지 볼 EMA
LONG_BARS = ("1D", "1W", "1M")  # 이 봉에서는 EMA7도 봄
RSI_MAX = 40           # 반등 후 RSI6이 이 값 이하일 때만 알림 (너무 오른 건 제외)
LIVE_RSI_MIN_CHG = 0.5 # 진행중 RSI 알림은 이번 봉이 이 % 이상 올랐을 때만 (약한 신호 제외)
EMA_MAX_PCT = 1.0      # EMA 위로 이 % 이내일 때만 알림 (너무 오른 건 제외)
EMA_SLOPE_BARS = 3     # EMA 돌파: EMA 선이 이 봉 수 전보다 낮으면(내려가는 중이면) 제외
TREND_SLOPE = 3        # 추세: EMA50이 이 봉 수 전보다 높아야 상승으로 인정
DD_BARS = 100          # 고점 대비 낙폭: 최근 몇 봉의 고점을 볼지
STOP_BARS = 10         # 접는 선: 최근 몇 봉의 저점을 볼지
STOP_BIG_PCT = 5       # 접는 선까지 거리가 이 % 이상이면 '손절폭 큼' 표시
VOL_MIN_ELAPSED = 0.1  # 진행중 봉 거래량 비교: 봉이 이 비율 이상 지났을 때만 계산
VOL_AVG_BARS = 20      # 거래량 평균을 낼 봉 개수 (평균 대비 ×배)
BASE_LOOKBACK = 48     # 바닥 횡보 돌파: 저점을 볼 봉 개수
BASE_RISE_PCT = 10     # 바닥 횡보 돌파: 저점 대비 이 % 이내일 때만
BASE_SPIKE_BARS = 24   # 바닥 횡보 돌파: 최근 몇 봉 안에 급등 이력이 있는지 볼 개수
BASE_SPIKE_PCT = 6     # 바닥 횡보 돌파: 최근 고점이 현재가보다 이 % 넘게 높았으면 제외 (이미 급등했다가 내려온 자리)
SQUEEZE_PCT = 3        # 바닥 횡보 돌파: 직전 10봉 EMA7·20·50이 평균 이 % 이내로 뭉쳐 있을 때만
WINDOW_MIN = 45        # 마감된 지 몇 분 이내의 봉만 알릴지
LOG_DAYS = 7           # 알림 기록을 며칠 보관할지
REPORT_HOUR = 9        # 매일 몇 시(한국시간)에 성적표를 보낼지
BARS = [("1H", "1시간봉"), ("2H", "2시간봉"), ("4H", "4시간봉"),
        ("1D", "일봉"), ("1W", "주봉"), ("1M", "월봉")]
BAR_MS = {"1H": 3600000, "2H": 7200000, "4H": 14400000,
          "1D": 86400000, "1W": 604800000, "1M": 2592000000}
STATE = "state/sent.json"
LOGFILE = "state/log.json"
HOURS = (1, 4, 24)
TREND_LABEL = {"up": "📈상승", "down": "📉하락", "mix": "↔혼조"}


def get(path, params=None):
    r = requests.get(OKX + path, params=params, timeout=15)
    r.raise_for_status()
    return r.json()["data"]


def rsi(closes, n):
    if len(closes) < n + 2:
        return []
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    out = []
    for i in range(n, len(gains) + 1):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return out


def ema(values, n):
    if len(values) < n + 1:
        return []
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    out = [e]
    for v in values[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def trend_code(closes):
    """추세: up / down / mix. 봉이 모자라면 None.
    up   = EMA50이 EMA200 위 + 가격이 EMA50 위 + EMA50이 오르는 중
    down = EMA50이 EMA200 아래 + 가격이 EMA200 아래
    mix  = 그 외"""
    try:
        e50 = ema(closes, 50)
        e200 = ema(closes, 200)
        if not e50 or not e200 or len(e50) <= TREND_SLOPE:
            return None
        p, a, b = closes[-1], e50[-1], e200[-1]
        if a < b and p < b:
            return "down"
        if a > b and p > b and p > a and a >= e50[-1 - TREND_SLOPE]:
            return "up"
        return "mix"
    except Exception:
        return None


def trend_text(tr):
    return "  " + TREND_LABEL[tr] if tr in TREND_LABEL else "  추세 -"


def ctx_info(cs, closes):
    """(위 저항 %, 저항 이름, 고점 대비 낙폭 %).
    저항 이름: 선 이름 / "없음"(가격 위에 선이 없음) / ""(계산 불가)"""
    try:
        p = closes[-1]
        lines = {}
        for n in (20, 50, 200):
            e = ema(closes, n)
            if e:
                lines[f"EMA{n}"] = e[-1]
        if len(cs) >= 100:
            v = [float(x[7]) for x in cs[-100:]]
            c = closes[-100:]
            sv = sum(v)
            if sv > 0:
                lines["VWMA100"] = sum(a * b for a, b in zip(c, v)) / sv
        above = {k: x for k, x in lines.items() if x > p}
        if not lines:
            rs, name = None, ""
        elif above:
            name = min(above, key=above.get)
            rs = (above[name] / p - 1) * 100
        else:
            rs, name = None, "없음"
        hi = max(float(x[2]) for x in cs[-DD_BARS:])
        dd = (p / hi - 1) * 100 if hi > 0 else None
        return rs, name, dd
    except Exception:
        return None, "", None


def ctx_text(rs, name, dd):
    parts = []
    if rs is not None:
        parts.append(f"위 저항 +{rs:.1f}%({name})")
    elif name == "없음":
        parts.append("위 저항 없음")
    if dd is not None:
        parts.append(f"고점 대비 {dd:.0f}%")
    return "  " + "  ".join(parts) if parts else ""


def stop_info(cs, price, ref=None):
    """접는 선: 최근 저점과 돌파한 EMA선 바로 아래 중 현재가에 더 가까운 쪽.
    (글자, 현재가 대비 거리 %) 를 돌려준다."""
    try:
        cands = [min(float(x[3]) for x in cs[-STOP_BARS:])]
        if ref:
            cands.append(ref)
        below = [c for c in cands if c < price]
        if not below:
            return "  접는 선 -", None
        s = max(below)
        pct = (s / price - 1) * 100
        t = f"  접는 선 {s:.6g}({pct:.1f}%)"
        if pct <= -STOP_BIG_PCT:
            t += " 손절폭 큼"
        return t, pct
    except Exception:
        return "  접는 선 -", None


def btc_line():
    """알림 맨 위에 붙일 비트코인 방향 한 줄."""
    try:
        parts = []
        chg = None
        for bar, label in (("1H", "1시간"), ("4H", "4시간")):
            c = get("/api/v5/market/candles",
                    {"instId": "BTC-USDT-SWAP", "bar": bar, "limit": "300"})
            closes = [float(x[4]) for x in c[::-1]]
            parts.append(f"{label} {TREND_LABEL.get(trend_code(closes), '-')}")
            if bar == "1H" and len(closes) > 25:
                chg = (closes[-1] / closes[-25] - 1) * 100
        if chg is not None:
            parts.append(f"24시간 {chg:+.1f}%")
        return "BTC " + " · ".join(parts)
    except Exception:
        return ""


def vol_info(cs, mode, bar, now):
    """(직전 봉 대비 %, 최근 20봉 평균 대비 ×배). 계산 못 하면 None.
    진행중 봉은 지금까지 쌓인 속도를 봉 전체 길이로 환산."""
    try:
        cur = float(cs[-1][7])
        prev = float(cs[-2][7])
        if mode == "live":
            dur = BAR_MS[bar]
            elapsed = now - int(cs[-1][0])
            if elapsed < dur * VOL_MIN_ELAPSED:
                return None, None
            cur = cur * dur / min(elapsed, dur)
        past = [float(x[7]) for x in cs[-1 - VOL_AVG_BARS:-1]]
        avg = sum(past) / len(past) if past else 0
        pct = (cur / prev - 1) * 100 if prev > 0 else None
        x = cur / avg if avg > 0 else None
        return pct, x
    except Exception:
        return None, None


def vol_text(pct, x):
    parts = []
    if pct is not None:
        parts.append(f"{'🔴' if pct >= 0 else '🔵'}직전 {pct:+.0f}%")
    if x is not None:
        parts.append(f"평균 ×{x:.1f}")
    return "  " + " · ".join(parts) if parts else "  거래량 -"


def telegram(token, chat, text):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for part in chunks:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": part.strip()},
            timeout=15,
        )


def isnum(v):
    return isinstance(v, (int, float))


def update_log(log, prices, now):
    """기록된 알림의 1·4·24시간 뒤 가격 변화를 채운다 (너무 늦으면 놓친 것으로 표시)."""
    for e in log:
        age = now - e["t"]
        p = prices.get(e["i"])
        for h in HOURS:
            key = f"r{h}"
            if e.get(key) is not None:
                continue
            if age < h * 3600000:
                continue
            if age > (h + 3) * 3600000 or not p or not e["p"]:
                e[key] = "x"
            else:
                e[key] = round((p / e["p"] - 1) * 100, 2)


def stat(items):
    n = len(items)
    up = sum(1 for e in items if e["r4"] > 0)
    avg = sum(e["r4"] for e in items) / n
    return f"{n}건 · 4시간 뒤 상승 {up * 100 // n}% · 평균 {avg:+.1f}%"


def make_report(log, now):
    rows = [e for e in log if isnum(e.get("r4")) and now - e["t"] <= LOG_DAYS * 86400000]
    if len(rows) < 5:
        return None
    lines = [f"📊 알림 성적표 (최근 {LOG_DAYS}일, 알림 가격 → 4시간 뒤 가격)", f"전체: {stat(rows)}", ""]

    def section(title, groups):
        out = []
        for name, items in groups:
            if items:
                out.append(f"  {name}: {stat(items)}")
        if out:
            lines.append(title)
            lines.extend(out)
            lines.append("")

    kn = {"rsi": "RSI 반등", "ema20": "EMA20 돌파", "ema50": "EMA50 돌파", "ema7": "EMA7 돌파", "base": "🎯바닥횡보"}
    section("■ 알림 종류", [(kn[k], [e for e in rows if e["k"] == k]) for k in kn])
    section("■ 추세", [("📈상승", [e for e in rows if e.get("tr") == "up"]),
                      ("📉하락", [e for e in rows if e.get("tr") == "down"]),
                      ("↔혼조", [e for e in rows if e.get("tr") == "mix"])])
    section("■ 거래량 (평균 대비)", [
        ("×0.5 미만", [e for e in rows if isnum(e.get("vx")) and e["vx"] < 0.5]),
        ("×0.5~1", [e for e in rows if isnum(e.get("vx")) and 0.5 <= e["vx"] < 1]),
        ("×1~2", [e for e in rows if isnum(e.get("vx")) and 1 <= e["vx"] < 2]),
        ("×2 이상", [e for e in rows if isnum(e.get("vx")) and e["vx"] >= 2])])
    section("■ 위 저항까지 거리", [
        ("2% 이내", [e for e in rows if isnum(e.get("rs")) and e["rs"] < 2]),
        ("2~5%", [e for e in rows if isnum(e.get("rs")) and 2 <= e["rs"] < 5]),
        ("5% 이상", [e for e in rows if isnum(e.get("rs")) and e["rs"] >= 5])])
    section("■ 고점 대비 낙폭", [
        ("-20% 이내", [e for e in rows if isnum(e.get("dd")) and e["dd"] > -20]),
        ("-20~-50%", [e for e in rows if isnum(e.get("dd")) and -50 < e["dd"] <= -20]),
        ("-50% 넘게 하락", [e for e in rows if isnum(e.get("dd")) and e["dd"] <= -50])])
    section("■ 접는 선까지 거리", [
        ("-2% 이내", [e for e in rows if isnum(e.get("sl")) and e["sl"] > -2]),
        ("-2~-5%", [e for e in rows if isnum(e.get("sl")) and -5 < e["sl"] <= -2]),
        ("-5% 넘게(손절폭 큼)", [e for e in rows if isnum(e.get("sl")) and e["sl"] <= -5])])
    section("■ 진행중 / 마감확정", [("진행중", [e for e in rows if e["m"] == "live"]),
                                    ("마감확정", [e for e in rows if e["m"] == "close"])])
    lines.append("건수가 적은 칸은 우연일 수 있어요. 며칠 더 쌓이면 믿을 만해져요.")
    return "\n".join(lines)


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    first = not os.path.exists(STATE)
    sent = {} if first else json.load(open(STATE))
    log = json.load(open(LOGFILE)) if os.path.exists(LOGFILE) else []
    now = int(time.time() * 1000)

    rows = []
    prices = {}
    for t in get("/api/v5/market/tickers", {"instType": "SWAP"}):
        if not t["instId"].endswith("-USDT-SWAP"):
            continue
        try:
            prices[t["instId"]] = float(t["last"])
            rows.append((float(t["volCcy24h"]) * float(t["last"]), t["instId"]))
        except ValueError:
            pass
    rows.sort(reverse=True)

    update_log(log, prices, now)

    def add_log(inst, bar, kind, mode, price, vp, vx, tr, rs, dd, sl):
        log.append({"t": now, "i": inst, "b": bar, "k": kind, "m": mode, "p": price,
                    "vp": None if vp is None else round(vp, 1),
                    "vx": None if vx is None else round(vx, 2),
                    "tr": tr,
                    "rs": None if rs is None else round(rs, 2),
                    "dd": None if dd is None else round(dd, 1),
                    "sl": None if sl is None else round(sl, 2),
                    "r1": None, "r4": None, "r24": None})

    hits = {label: [] for _, label in BARS}
    bases = []
    for vol, inst in rows[:TOP_N]:
        name = inst.replace("-USDT-SWAP", "")
        for bar, label in BARS:
            time.sleep(0.15)
            try:
                c = get("/api/v5/market/candles",
                        {"instId": inst, "bar": bar, "limit": "300"})
            except Exception:
                continue
            if not c:
                continue
            live = c[0] if c[0][8] == "0" else None          # 진행 중인 봉
            done = [x for x in c if x[8] == "1"][::-1]       # 마감된 봉, 오래된 순
            if len(done) < 8:
                continue
            # 방금 마감된 봉인지 (진행 중인 봉의 시작 시각 = 직전 봉의 마감 시각)
            close_time = int(live[0]) if live else now
            recent = now - close_time <= WINDOW_MIN * 60000

            cases = []   # (모드, 봉 목록)
            if recent:
                cases.append(("close", done))
            if live:
                cases.append(("live", done + [live]))

            for mode, cs in cases:
                ts = cs[-1][0]
                closes = [float(x[4]) for x in cs]
                chg = (closes[-1] / float(cs[-1][1]) - 1) * 100 if float(cs[-1][1]) else 0
                vp, vx = vol_info(cs, mode, bar, now)
                tr = trend_code(closes)
                rs, rname, dd = ctx_info(cs, closes)
                vt = vol_text(vp, vx) + trend_text(tr) + ctx_text(rs, rname, dd)

                found = []   # (종류, 문구, 접는 선 거리%)
                r = rsi(closes, RSI_SIGNAL)
                if len(r) >= 2 and r[-2] <= LOW < r[-1] <= RSI_MAX and (mode == "close" or chg >= LIVE_RSI_MIN_CHG):
                    shown = []
                    for n in RSI_SHOW:
                        rr = rsi(closes, n)
                        shown.append(f"{rr[-1]:.0f}" if rr else "-")
                    st, sl = stop_info(cs, closes[-1])
                    found.append(("rsi", f"{name}  RSI{RSI_SIGNAL} {r[-2]:.1f}→{r[-1]:.1f}  (RSI {'/'.join(shown)})  이번봉 {chg:+.1f}%{vt}{st}", sl))
                for n in ((7,) + EMAS if bar in LONG_BARS else EMAS):
                    e = ema(closes, n)
                    if len(e) > EMA_SLOPE_BARS and closes[-2] <= e[-2] and closes[-1] > e[-1]:
                        if e[-1] < e[-1 - EMA_SLOPE_BARS]:
                            continue  # EMA 선이 내려가는 중이면 제외
                        above = (closes[-1] / e[-1] - 1) * 100
                        if above <= EMA_MAX_PCT:
                            st, sl = stop_info(cs, closes[-1], e[-1] * 0.998)
                            found.append((f"ema{n}", f"{name}  EMA{n} 위 +{above:.1f}%  이번봉 {chg:+.1f}%{vt}{st}", sl))

                # 🎯 바닥 횡보 돌파: 저점 근처 + EMA 뭉침 + EMA20 위로 첫 돌파
                if len(cs) >= 60:
                    e7, e20, e50 = ema(closes, 7), ema(closes, 20), ema(closes, 50)
                    if len(e50) >= 12 and closes[-2] <= e20[-2] and closes[-1] > e20[-1]:
                        spread = []
                        for i in range(2, 12):
                            a, b, c3 = e7[-i], e20[-i], e50[-i]
                            spread.append((max(a, b, c3) - min(a, b, c3)) / b * 100)
                        squeeze = sum(spread) / len(spread)
                        low_n = min(float(x[3]) for x in cs[-BASE_LOOKBACK:])
                        rise = (closes[-1] / low_n - 1) * 100 if low_n > 0 else 999
                        high_n = max(float(x[2]) for x in cs[-BASE_SPIKE_BARS:])
                        spike = (high_n / closes[-1] - 1) * 100
                        if squeeze <= SQUEEZE_PCT and rise <= BASE_RISE_PCT and spike <= BASE_SPIKE_PCT:
                            bkey = f"{inst}|{bar}|base" + ("|live" if mode == "live" else "")
                            if sent.get(bkey) != ts:
                                btag = "진행중" if mode == "live" else "마감확정"
                                st, sl = stop_info(cs, closes[-1], e20[-1] * 0.998)
                                bases.append(f"[{label}·{btag}] {name}  저점 대비 +{rise:.1f}%  EMA뭉침 {squeeze:.1f}%  최근고점 +{spike:.1f}%  이번봉 {chg:+.1f}%{vt}{st}")
                                sent[bkey] = ts
                                add_log(inst, bar, "base", mode, closes[-1], vp, vx, tr, rs, dd, sl)

                for kind, line, sl in found:
                    key = f"{inst}|{bar}|{kind}" + ("|live" if mode == "live" else "")
                    if sent.get(key) != ts:
                        what = "RSI 반등" if kind == "rsi" else f"{kind.upper()} 돌파"
                        tag = f"{what}·진행중" if mode == "live" else f"{what}·마감확정"
                        hits[label].append(f"[{tag}] {line}")
                        sent[key] = ts
                        add_log(inst, bar, kind, mode, closes[-1], vp, vx, tr, rs, dd, sl)

    parts = [f"━ {label} ━\n" + "\n".join(v) for label, v in hits.items() if v]
    if bases:
        parts.insert(0, "🎯 바닥 횡보 돌파 🎯\n" + "\n".join(bases))
    if parts:
        btc = btc_line()
        if btc:
            parts.insert(0, btc)
        telegram(token, chat, "\n\n".join(parts))
    elif first:
        telegram(token, chat, "✅ RSI 알림이 켜졌어요. 조건에 맞는 코인이 나오면 알려 드려요.")

    # 매일 아침 성적표
    kst = time.gmtime(now / 1000 + 9 * 3600)
    today = time.strftime("%Y-%m-%d", kst)
    if kst.tm_hour == REPORT_HOUR and sent.get("_report") != today:
        text = make_report(log, now)
        if text:
            telegram(token, chat, text)
            sent["_report"] = today

    log = [e for e in log if now - e["t"] <= LOG_DAYS * 86400000]
    os.makedirs("state", exist_ok=True)
    json.dump(sent, open(STATE, "w"))
    json.dump(log, open(LOGFILE, "w"))


if __name__ == "__main__":
    main()
