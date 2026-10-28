import os, json, time, requests

OKX = "https://www.okx.com"
TOP_N = 100            # 거래대금 상위 몇 개를 볼지
LOW = 30               # RSI 바닥 기준
RSI_SIGNAL = 6         # 알림 기준 RSI (차트의 RSI6)
RSI_SHOW = (6, 12, 24) # 알림에 같이 보여 줄 RSI
EMAS = (20, 50)        # 가격이 위로 뚫는지 볼 EMA
LONG_BARS = ("1D", "1W", "1M")  # 이 봉에서는 EMA7도 봄
WINDOW_MIN = 45        # 마감된 지 몇 분 이내의 봉만 알릴지
BARS = [("1H", "1시간봉"), ("2H", "2시간봉"), ("4H", "4시간봉"),
        ("1D", "일봉"), ("1W", "주봉"), ("1M", "월봉")]
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


def telegram(token, chat, text):
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat, "text": text},
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
            # 방금 마감된 봉인지 확인 (진행 중인 봉의 시작 시각 = 직전 봉의 마감 시각)
            close_time = int(c[0][0]) if c[0][8] == "0" else now
            if now - close_time > WINDOW_MIN * 60000:
                continue
            c = [x for x in c if x[8] == "1"][::-1]  # 마감된 봉만, 오래된 순
            if len(c) < 8:
                continue
            ts = c[-1][0]
            closes = [float(x[4]) for x in c]

            found = []
            r = rsi(closes, RSI_SIGNAL)
            if len(r) >= 2 and r[-2] <= LOW < r[-1]:
                shown = []
                for n in RSI_SHOW:
                    rr = rsi(closes, n)
                    shown.append(f"{rr[-1]:.0f}" if rr else "-")
                found.append(("rsi", f"{name}  RSI{RSI_SIGNAL} {r[-2]:.1f}→{r[-1]:.1f}  (RSI {'/'.join(shown)})"))
            for n in ((7,) + EMAS if bar in LONG_BARS else EMAS):
                e = ema(closes, n)
                if len(e) >= 2 and closes[-2] <= e[-2] and closes[-1] > e[-1]:
                    found.append((f"ema{n}", f"{name}  EMA{n} 돌파"))

            for kind, line in found:
                key = f"{inst}|{bar}|{kind}"
                if sent.get(key) != ts:
                    tag = "RSI 반등" if kind == "rsi" else f"{kind.upper()} 돌파"
                    hits[label].append(f"[{tag}] {line}")
                    sent[key] = ts

    parts = [f"━ {label} ━\n" + "\n".join(v) for label, v in hits.items() if v]
    if parts:
        telegram(token, chat, "\n\n".join(parts))
    elif first:
        telegram(token, chat, "✅ RSI 알림이 켜졌어요. 조건에 맞는 코인이 나오면 알려 드려요.")

    os.makedirs("state", exist_ok=True)
    json.dump(sent, open(STATE, "w"))


if __name__ == "__main__":
    main()
