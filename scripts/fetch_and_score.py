#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
株ランキング計算スクリプト（試作 v1）

- データ取得: Yahoo Finance chart API（キー不要、標準ライブラリのみ）
  例: https://query1.finance.yahoo.com/v8/finance/chart/7203.T?range=6mo&interval=1d
- 対象: 試作では大型・高流動性の日本株50銘柄。本実装では日経225全銘柄へ拡張する。
- 出力: data/ranking.json（静的サイトが読み込んで表示する）

スコア（100点満点）※詳細は docs/SPEC.md
  1. 相対強さ RS（25点）   : 指数より下げていないか、直近20日の相対推移
  2. 売られ過ぎ度（20点） : RSI(14)、25日移動平均乖離率。ただし下げ過ぎは「落ちるナイフ」として減点
  3. 反転サイン（20点）   : 下ヒゲ、当日の値位置、安値更新の有無などのローソク足形状
  4. トレンド健全性（15点）: 75日線との位置、25日線>75日線、6か月高値からの下落率
  5. 流動性・リスク（10点）: 売買代金、ATR%（ボラが高すぎる銘柄は減点）
  6. 下げ止まり（10点）   : 5日騰落と20日騰落の改善、陽線化

市況レジーム:
  日経平均が -3%以下 → 「全面安モード」。ランキングは「買え」ではなく
  「反発候補（監視）」として表示し、無効条件（終値で直近安値更新）を必ず併記する。

注意: このスコアはスクリーニング（候補の絞り込み）であり、検証済みの利益予測ではない。
"""
import json
import math
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

NIKKEI_CSV_URL = "https://indexes.nikkei.co.jp/nkave/archives/file/nikkei_225_price_adjustment_factor_jp.csv"


def load_universe():
    """日経公式の構成銘柄CSV（株価換算係数一覧）から日経225全銘柄を取得する。

    公式CSVは Shift_JIS、対象日付つきで日々更新される。取得できない場合は
    試作用の大型50銘柄にフォールバックし、その旨を出力JSONに明記する。
    戻り値: (銘柄リスト[(code, name, sector)], 出所ラベル)
    """
    import csv
    import io
    import unicodedata
    try:
        req = urllib.request.Request(NIKKEI_CSV_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("cp932")
        rows = list(csv.reader(io.StringIO(text)))
        out, dates = [], set()
        for row in rows[1:]:
            if len(row) >= 5 and row[1].strip() and row[1].strip()[0].isdigit():
                dates.add(row[0])
                name = unicodedata.normalize("NFKC", row[2]).strip()
                out.append((row[1].strip(), name, row[4].strip()))
        if len(out) == 225 and len({c for c, _, _ in out}) == 225:
            return out, f"日経公式CSV（対象日付 {sorted(dates)[-1]}）"
        print(f"公式CSVの件数が想定外: {len(out)}件", file=sys.stderr)
    except Exception as e:
        print(f"公式CSV取得失敗: {e}", file=sys.stderr)
    return [(c, n, "") for c, n in FALLBACK_UNIVERSE], "フォールバック（試作用50銘柄）"


# フォールバック用: 大型・高流動性50銘柄（コード, 名称）
FALLBACK_UNIVERSE = [
    ("7203", "トヨタ自動車"), ("6758", "ソニーグループ"), ("8306", "三菱UFJ"),
    ("9984", "ソフトバンクグループ"), ("8035", "東京エレクトロン"), ("6857", "アドバンテスト"),
    ("9983", "ファーストリテイリング"), ("6861", "キーエンス"), ("6098", "リクルートHD"),
    ("9432", "NTT"), ("8316", "三井住友FG"), ("6367", "ダイキン工業"),
    ("6501", "日立製作所"), ("8058", "三菱商事"), ("8001", "伊藤忠商事"),
    ("9433", "KDDI"), ("8411", "みずほFG"), ("7974", "任天堂"),
    ("4502", "武田薬品工業"), ("4519", "中外製薬"), ("7267", "ホンダ"),
    ("7011", "三菱重工業"), ("5803", "フジクラ"), ("7741", "HOYA"),
    ("6954", "ファナック"), ("6981", "村田製作所"), ("6762", "TDK"),
    ("8766", "東京海上HD"), ("8591", "オリックス"), ("2914", "JT"),
    ("9020", "JR東日本"), ("4661", "オリエンタルランド"), ("3382", "セブン&アイHD"),
    ("9202", "ANA HD"), ("5401", "日本製鉄"), ("1605", "INPEX"),
    ("4901", "富士フイルムHD"), ("6594", "ニデック"), ("6723", "ルネサスエレクトロニクス"),
    ("6146", "ディスコ"), ("7733", "オリンパス"), ("4568", "第一三共"),
    ("6503", "三菱電機"), ("7751", "キヤノン"), ("8801", "三井不動産"),
    ("1925", "大和ハウス工業"), ("4063", "信越化学工業"), ("8031", "三井物産"),
    ("6752", "パナソニックHD"), ("5108", "ブリヂストン"),
]

INDEX_SYMBOL = "^N225"
INDEX_NAME = "日経平均"


def bar_date(ts):
    return datetime.fromtimestamp(ts, JST).date()


def fetch_chart(symbol: str, range_: str = "6mo") -> dict:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(symbol)
        + f"?range={range_}&interval=1d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    res = d["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(res.get("timestamp", [])):
        if q["close"][i] is None:
            continue
        bars.append({
            "t": ts,
            "o": q["open"][i], "h": q["high"][i],
            "l": q["low"][i], "c": q["close"][i], "v": q["volume"][i] or 0,
        })
    return {"meta": res["meta"], "bars": bars}


def sma(vals, n, end=None):
    if end is None:
        end = len(vals)
    if end < n:
        return None
    return sum(vals[end - n:end]) / n


def rsi14(closes):
    if len(closes) < 15:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    # 直近14日で計算（簡易版）
    ag = sum(gains[-14:]) / 14
    al = sum(losses[-14:]) / 14
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def atr_pct(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])))
    atr = sum(trs[-n:]) / n
    return atr / bars[-1]["c"] * 100 if bars[-1]["c"] else None


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def score_stock(bars, index_chg_today, index_closes):
    closes = [b["c"] for b in bars]
    vols = [b["v"] for b in bars]
    last, prev = bars[-1], bars[-2]
    chg = (last["c"] / prev["c"] - 1) * 100
    rsi = rsi14(closes)
    ma25 = sma(closes, 25)
    ma75 = sma(closes, 75)
    dev25 = (last["c"] / ma25 - 1) * 100 if ma25 else 0.0
    high6m = max(closes)
    dd_high = (last["c"] / high6m - 1) * 100
    atrp = atr_pct(bars)
    # 相対強さ
    rs_today = chg - index_chg_today
    n = min(20, len(closes) - 1, len(index_closes) - 1)
    rs_20d = ((closes[-1] / closes[-1 - n] - 1) - (index_closes[-1] / index_closes[-1 - n] - 1)) * 100
    # ローソク足形状
    rng = last["h"] - last["l"]
    lower_wick = (min(last["o"], last["c"]) - last["l"]) / rng if rng > 0 else 0.0
    close_pos = (last["c"] - last["l"]) / rng if rng > 0 else 0.5
    low20 = min(b["l"] for b in bars[-20:])
    is_new_low = last["l"] <= low20 and last["l"] < min(b["l"] for b in bars[-21:-1]) if len(bars) >= 21 else False
    # 出来高
    vavg20 = sma(vols, 20, end=len(vols) - 1)  # 当日を除く20日平均
    vol_ratio = last["v"] / vavg20 if vavg20 else 1.0
    turnover = last["c"] * last["v"]  # 当日売買代金（概算、ザラ場中は途中経過）
    # 騰落
    ret5 = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else 0.0
    ret20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 else 0.0

    reasons, risks = [], []

    # 1. 相対強さ 25点
    s_rs = clamp(12.5 + rs_today * 2.2, 0, 18) + clamp(3.5 + rs_20d * 0.35, 0, 7)
    if rs_today >= 2:
        reasons.append(f"指数より{rs_today:+.1f}pt強い（下げ耐性）")
    elif rs_today <= -2:
        risks.append(f"指数より{abs(rs_today):.1f}pt弱い")

    # 2. 売られ過ぎ 20点（RSI 28〜45と乖離 -12〜-3%が加点帯、それ以下は落下扱い）
    if rsi is None:
        s_os = 8
    elif 28 <= rsi <= 45:
        s_os = 14
        reasons.append(f"RSI {rsi:.0f}（売られ過ぎ圏）")
    elif 20 <= rsi < 28:
        s_os = 10
        reasons.append(f"RSI {rsi:.0f}（深い売られ過ぎ）")
    elif rsi < 20:
        s_os = 5
        risks.append(f"RSI {rsi:.0f}（落下中、反転未確認）")
    elif 45 < rsi <= 55:
        s_os = 9
    else:
        s_os = 5
    if -12 <= dev25 <= -3:
        s_os += 6
        reasons.append(f"25日線乖離 {dev25:+.1f}%")
    elif dev25 < -15:
        s_os += 1
        risks.append(f"25日線乖離 {dev25:+.1f}%（下げ過ぎ）")
    elif dev25 < -12:
        s_os += 3
    s_os = clamp(s_os, 0, 20)

    # 3. 反転サイン 20点
    s_rev = 6.0
    if lower_wick >= 0.5:
        s_rev += 7
        reasons.append(f"長い下ヒゲ（下ヒゲ比率{lower_wick:.0%}）")
    elif lower_wick >= 0.35:
        s_rev += 4
        reasons.append("下ヒゲあり（安値で買い）")
    if close_pos >= 0.7:
        s_rev += 4
        reasons.append("当日レンジの上部で引け")
    if last["c"] > last["o"]:
        s_rev += 3
    if is_new_low and close_pos < 0.4:
        s_rev -= 6
        risks.append("直近安値を更新して弱い引け（落ちるナイフ注意）")
    s_rev = clamp(s_rev, 0, 20)

    # 4. トレンド健全性 15点
    s_tr = 5.0
    if ma75 and last["c"] >= ma75:
        s_tr += 5
        reasons.append("75日線の上を維持")
    elif ma75 and last["c"] >= ma75 * 0.95:
        s_tr += 2.5
    if ma25 and ma75 and ma25 > ma75:
        s_tr += 3
    if -20 <= dd_high <= -5:
        s_tr += 2
    elif dd_high < -35:
        s_tr -= 2
        risks.append(f"6か月高値から{dd_high:.0f}%（中期トレンド崩壊圏）")
    s_tr = clamp(s_tr, 0, 15)

    # 5. 流動性・リスク 10点
    s_lq = 3.0
    if turnover >= 10_000_000_000:
        s_lq += 4
    elif turnover >= 3_000_000_000:
        s_lq += 2.5
    if atrp is not None:
        if 1.5 <= atrp <= 4.0:
            s_lq += 3
        elif atrp > 7:
            s_lq -= 1
            risks.append(f"ATR {atrp:.1f}%（値動きが荒い）")
        else:
            s_lq += 1.5
    s_lq = clamp(s_lq, 0, 10)

    # 6. 下げ止まり 10点
    s_st = 4.0
    if ret5 > ret20 / max(1, 20 / 5) if ret20 else False:
        pass
    if ret5 >= -2 and ret20 <= -5:
        s_st += 4
        reasons.append("20日の下げに対して直近5日の下げが鈍化")
    if vol_ratio >= 1.3 and chg > -1:
        s_st += 2
        reasons.append(f"出来高 {vol_ratio:.1f}倍で下げ止まり")
    elif vol_ratio >= 1.5 and chg <= -4:
        risks.append(f"出来高 {vol_ratio:.1f}倍の大陰線（投げ売り未了の可能性）")
    s_st = clamp(s_st, 0, 10)

    total = round(s_rs + s_os + s_rev + s_tr + s_lq + s_st, 1)
    return {
        "price": last["c"], "change_pct": round(chg, 2),
        "rsi": round(rsi, 1) if rsi is not None else None,
        "dev25_pct": round(dev25, 1),
        "atr_pct": round(atrp, 1) if atrp is not None else None,
        "vol_ratio": round(vol_ratio, 2),
        "dd_from_high_pct": round(dd_high, 1),
        "spark": [round(c, 1) for c in closes[-30:]],
        "score": total,
        "breakdown": {
            "relative_strength": round(s_rs, 1),
            "oversold": round(s_os, 1),
            "reversal": round(s_rev, 1),
            "trend": round(s_tr, 1),
            "liquidity_risk": round(s_lq, 1),
            "stabilizing": round(s_st, 1),
        },
        "reasons": reasons[:4],
        "risks": risks[:3],
        "invalid_if": "終値で直近安値を更新したら候補から外す",
    }


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "data/ranking.json"
    idx = fetch_chart(INDEX_SYMBOL)
    idx_bars = idx["bars"]
    idx_closes = [b["c"] for b in idx_bars]
    idx_chg = (idx_bars[-1]["c"] / idx_bars[-2]["c"] - 1) * 100

    idx_last_date = bar_date(idx_bars[-1]["t"])
    universe, universe_source = load_universe()
    print(f"ユニバース: {len(universe)}銘柄（{universe_source}）")

    def job(item):
        code, name, sector = item
        try:
            d = fetch_chart(f"{code}.T")
            bars = d["bars"]
            # --- データ品質ガード（試作で実際に踏んだ2件）---
            # 1) 陳腐化: 最終バーの日付が指数と違う銘柄は「今日」として扱わない（例: JTは当日バーがNoneで前日分が混入）
            if bar_date(bars[-1]["t"]) != idx_last_date:
                return {"code": code, "name": name,
                        "error": f"データ遅延: 最終バー {bar_date(bars[-1]['t'])} ≠ 指数 {idx_last_date}"}
            # 2) 急変は除外しない（検証結果: 2026-11-05 のトヨタ +16.7% は決算上方修正による実在の値動きだった）。
            #    代わりに「急変フラグ」として理由欄に併記し、材料確認を促す。
            closes = [b["c"] for b in bars]
            jumps = []
            for i in range(max(1, len(closes) - 3), len(closes)):
                ch = (closes[i] / closes[i - 1] - 1) * 100
                if abs(ch) > 10:
                    jumps.append(f"{bar_date(bars[i]['t'])} に {ch:+.1f}%")
            sc = score_stock(bars, idx_chg, idx_closes)
            if jumps:
                sc["risks"] = (sc["risks"] + [f"直近の急変あり（{'、'.join(jumps)}）―決算等の材料か要確認"])[:3]
            sc.update({"code": code, "name": name, "sector": sector})
            return sc
        except Exception as e:
            return {"code": code, "name": name, "error": str(e)}

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(job, universe))

    ok = [r for r in results if "score" in r]
    ng = [r for r in results if "score" not in r]
    ok.sort(key=lambda x: x["score"], reverse=True)
    adv = sum(1 for r in ok if r["change_pct"] > 0)
    regime = "panic" if idx_chg <= -3 else ("weak" if idx_chg <= -1 else "normal")
    payload = {
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "market": {
            "index_name": INDEX_NAME,
            "index_value": idx_bars[-1]["c"],
            "index_change_pct": round(idx_chg, 2),
            "regime": regime,
            "regime_label": {"panic": "全面安モード（反発候補として表示）",
                             "weak": "軟調モード", "normal": "通常モード"}[regime],
            "universe_size": len(ok),
            "universe_source": universe_source,
            "advancers": adv,
            "decliners": len(ok) - adv,
            "note": "ザラ場中に生成した場合は当日の出来高・終値は途中経過",
        },
        "disclaimer": "このランキングは指標に基づくスクリーニングであり、利益を保証する買い推奨ではありません。スコアは検証済みの予測モデルではなく、根拠とリスクを必ず併記して表示します。",
        "ranking": ok,
        "errors": ng,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"OK {len(ok)} / NG {len(ng)} -> {out_path}")
    for r in ok[:10]:
        print(f'{r["code"]} {r["name"]} score={r["score"]} chg={r["change_pct"]:+.2f}% RSI={r["rsi"]}')


if __name__ == "__main__":
    main()
