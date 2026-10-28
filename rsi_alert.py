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
VOL_MIN_ELAPSED = 0.1  # 진행중 봉 거래량 비교: 봉이 이 비율 이상 지났을 때만 계산
VOL_AVG_BARS = 20      # 거래량 평균을 낼 봉 개수 (평균 대비 ×배)
BASE_LOOKBACK = 48     # 바닥 횡보 돌파: 저점을 볼 봉 개수
BASE_RISE_PCT = 10     # 바닥 횡보 돌파: 저점 대비 이 % 이내일 때만
BASE_SPIKE_BARS = 24   # 바닥 횡보 돌파: 최근 몇 봉 안에 급등 이력이 있는지 볼 개수
BASE_SPIKE_PCT = 6     # 바닥 횡보 돌파: 최근 고점이 현재가보다 이 % 넘게 높았으면 제외 (이미 급등했다가 내려온 자리)
SQUEEZE_PCT = 3        # 바닥 횡보 돌파: 직전 10봉 EMA7·20·50이 평균 이 % 이내로 뭉쳐 있을 때만
WINDOW_MIN = 45        # 마감된 지 몇 분 이내의 봉만 알릴지
BARS = [("1H", "1시간봉"), ("2H", "2시간봉"), ("4H", "4시간봉"),
        ("1D", "일봉"), ("1W", "주봉"), ("1M", "월봉")]
BAR_MS = {"1H": 3600000, "2H": 7200000, "4H": 14400000,
          "1D": 86400000, "1W": 604800000, "1M": 2592000000}
STATE = "state/sent.json"


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


def trend_text(closes):
    """추세: EMA50·EMA200·가격 위치로 판단. 📈상승 / 📉하락 / ↔혼조. 봉이 모자라면 추세 -"""
    try:
        e50 = ema(closes, 50)
        e200 = ema(closes, 200)
        if not e50 or not e200:
            return "  추세 -"
        p, a, b = closes[-1], e50[-1], e200[-1]
        if a > b and p > b:
            return "  📈상승"
        if a < b and p < b:
            return "  📉하락"
        return "  ↔혼조"
    except Exception:
        return "  추세 -"


def vol_text(cs, mode, bar, now):
    """거래량: 직전 봉 대비 %(늘면 🔴, 줄면 🔵) + 최근 20봉 평균 대비 ×배.
    진행중 봉은 지금까지 쌓인 속도를 봉 전체 길이로 환산."""
    try:
        cur = float(cs[-1][7])
        prev = float(cs[-2][7])
        if mode == "live":
            dur = BAR_MS[bar]
            elapsed = now - int(cs[-1][0])
            if elapsed < dur * VOL_MIN_ELAPSED:
                return "  거래량 -"
            cur = cur * dur / min(elapsed, dur)
        past = [float(x[7]) for x in cs[-1 - VOL_AVG_BARS:-1]]
        avg = sum(past) / len(past) if past else 0
        parts = []
        if prev > 0:
            pct = (cur / prev - 1) * 100
            icon = "🔴" if pct >= 0 else "🔵"
            parts.append(f"{icon}직전 {pct:+.0f}%")
        if avg > 0:
            parts.append(f"평균 ×{cur / avg:.1f}")
        if not parts:
            return "  거래량 -"
        return "  " + " · ".join(parts)
    except Exception:
        return "  거래량 -"


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


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    first = not os.path.exists(STATE)
    sent = {} if first else json.load(open(STATE))
    now = int(time.time() * 1000)

    rows = []
    for t in get("/api/v5/market/tickers", {"instType": "SWAP"}):
        if not t["instId"].endswith("-USDT-SWAP"):
            continue
        try:
            rows.append((float(t["volCcy24h"]) * float(t["last"]), t["instId"]))
        except ValueError:
            pass
    rows.sort(reverse=True)

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
                vt = vol_text(cs, mode, bar, now) + trend_text(closes)

                found = []
                r = rsi(closes, RSI_SIGNAL)
                if len(r) >= 2 and r[-2] <= LOW < r[-1] <= RSI_MAX and (mode == "close" or chg >= LIVE_RSI_MIN_CHG):
                    shown = []
                    for n in RSI_SHOW:
                        rr = rsi(closes, n)
                        shown.append(f"{rr[-1]:.0f}" if rr else "-")
                    found.append(("rsi", f"{name}  RSI{RSI_SIGNAL} {r[-2]:.1f}→{r[-1]:.1f}  (RSI {'/'.join(shown)})  이번봉 {chg:+.1f}%{vt}"))
                for n in ((7,) + EMAS if bar in LONG_BARS else EMAS):
                    e = ema(closes, n)
                    if len(e) > EMA_SLOPE_BARS and closes[-2] <= e[-2] and closes[-1] > e[-1]:
                        if e[-1] < e[-1 - EMA_SLOPE_BARS]:
                            continue  # EMA 선이 내려가는 중이면 제외
                        above = (closes[-1] / e[-1] - 1) * 100
                        if above <= EMA_MAX_PCT:
                            found.append((f"ema{n}", f"{name}  EMA{n} 위 +{above:.1f}%  이번봉 {chg:+.1f}%{vt}"))

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
                                bases.append(f"[{label}·{btag}] {name}  저점 대비 +{rise:.1f}%  EMA뭉침 {squeeze:.1f}%  최근고점 +{spike:.1f}%  이번봉 {chg:+.1f}%{vt}")
                                sent[bkey] = ts

                for kind, line in found:
                    key = f"{inst}|{bar}|{kind}" + ("|live" if mode == "live" else "")
                    if sent.get(key) != ts:
                        what = "RSI 반등" if kind == "rsi" else f"{kind.upper()} 돌파"
                        tag = f"{what}·진행중" if mode == "live" else f"{what}·마감확정"
                        hits[label].append(f"[{tag}] {line}")
                        sent[key] = ts

    parts = [f"━ {label} ━\n" + "\n".join(v) for label, v in hits.items() if v]
    if bases:
        parts.insert(0, "🎯 바닥 횡보 돌파 🎯\n" + "\n".join(bases))
    if parts:
        telegram(token, chat, "\n\n".join(parts))
    elif first:
        telegram(token, chat, "✅ RSI 알림이 켜졌어요. 조건에 맞는 코인이 나오면 알려 드려요.")

    os.makedirs("state", exist_ok=True)
    json.dump(sent, open(STATE, "w"))


if __name__ == "__main__":
    main()
