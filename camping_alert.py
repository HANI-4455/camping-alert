"""대전상소오토캠핑장 토·일 입실 빈자리 텔레그램 알림.

사용법:
  python camping_alert.py          # 1회 확인 (작업 스케줄러가 5분마다 실행)
  python camping_alert.py --test   # 텔레그램 테스트 메시지 + 현재 빈자리 출력
"""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import requests

URL = "https://www.sangsocamping.kr:453/reservation.asp?location=002"
ZONES = {1: "A구역(파쇄석)", 2: "B구역(강자갈)", 3: "C구역(강자갈)", 4: "D구역(파쇄석)", 5: "E구역(데크)"}
WEEKEND = {5: "토", 6: "일"}  # 입실 요일
MAX_MONTHS = 3

BASE = Path(__file__).parent
# GitHub Actions에서는 Secrets(환경변수), PC에서는 config.json
CONFIG = {"telegram_token": os.environ.get("TELEGRAM_TOKEN"),
          "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID")}
if not CONFIG["telegram_token"]:
    CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
STATE_FILE = BASE / "state.json"
LOG_FILE = BASE / "log.txt"

# rsv_info 예: A#@3001#@2026-10-03#@2026-10-04#@0#@0  (+ 버튼 텍스트 A구역01)
SLOT_RE = re.compile(r'name="rsv_info" value="[^#]*#@\d+#@(\d{4}-\d{2}-\d{2})#@[^"]*"\s*/>\s*'
                     r'<button[^>]*class="rsv_ok"[^>]*>.*?/>\s*([^<*]+?)\s*\*?</button>', re.S)


def log(msg):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(year, month, zone):
    r = requests.post(URL, data={"wh_year": year, "wh_month": month, "man": zone},
                      headers={"User-Agent": "Mozilla/5.0"}, timeout=20, verify=False)
    r.raise_for_status()
    return r.content.decode("cp949", errors="replace")


def available_slots():
    """{ '2026-10-03|A구역01', ... } 오늘 이후 토·일 입실 예약가능 사이트."""
    today = dt.date.today()
    slots = set()
    for zone in ZONES:
        y, m = today.year, today.month
        for _ in range(MAX_MONTHS):
            html = fetch(y, m, zone)
            for date_s, site in SLOT_RE.findall(html):
                d = dt.date.fromisoformat(date_s)
                if d >= today and d.weekday() in WEEKEND:
                    slots.add(f"{date_s}|{site.strip()}")
            if 'name="form_next"' not in html:  # 다음 달이 아직 안 열림
                break
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return slots


def send_telegram(text):
    r = requests.post(f"https://api.telegram.org/bot{CONFIG['telegram_token']}/sendMessage",
                      data={"chat_id": CONFIG["telegram_chat_id"], "text": text,
                            "disable_web_page_preview": True}, timeout=20)
    r.raise_for_status()


def format_msg(slots):
    by_date = {}
    for s in sorted(slots):
        date_s, site = s.split("|")
        by_date.setdefault(date_s, []).append(site)
    lines = ["⛺ 상소동 캠핑장 주말 빈자리!"]
    for date_s, sites in by_date.items():
        d = dt.date.fromisoformat(date_s)
        lines.append(f"\n{d:%m/%d}({WEEKEND[d.weekday()]}) 입실 - {len(sites)}자리")
        lines.append("  " + ", ".join(sites))
    lines.append(f"\n예약: {URL}")
    return "\n".join(lines)


def main():
    requests.packages.urllib3.disable_warnings()
    now = available_slots()

    if "--test" in sys.argv:
        print(format_msg(now) if now else "현재 토·일 입실 빈자리 없음")
        send_telegram("✅ 캠핑장 알림 테스트 성공\n현재 토·일 빈자리 " + f"{len(now)}건")
        print("텔레그램 전송 완료")
        return

    prev = set(json.loads(STATE_FILE.read_text(encoding="utf-8"))) if STATE_FILE.exists() else None
    new = now - prev if prev is not None else now
    if new:
        send_telegram(format_msg(new))
        log(f"알림 전송: 새 빈자리 {len(new)}건 (전체 {len(now)}건)")
    else:
        log(f"새 빈자리 없음 (전체 {len(now)}건)")
    STATE_FILE.write_text(json.dumps(sorted(now), ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"오류: {e!r}")
        raise
