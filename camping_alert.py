"""대전상소오토캠핑장 토·일 입실 빈자리 텔레그램 알림.

사용법:
  python camping_alert.py          # 1회 확인 (GitHub Actions가 매시간 실행)
  python camping_alert.py --test   # 텔레그램 테스트 메시지 + 현재 빈자리 출력
텔레그램 명령: /on 켜기, /off 끄기, /status 상태 (다음 정기 실행 때 반영)
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
KST = dt.timezone(dt.timedelta(hours=9))  # GitHub 서버는 UTC라서 한국 시간 기준으로 계산

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


class SiteClosed(Exception):
    pass


def log(msg):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(year, month, zone):
    r = requests.post(URL, data={"wh_year": year, "wh_month": month, "man": zone},
                      headers={"User-Agent": "Mozilla/5.0"}, timeout=20, verify=False)
    r.raise_for_status()
    html = r.content.decode("cp949", errors="replace")
    if "calendar_t" not in html:  # 매월 1일 10시 전 등 사이트가 안내 페이지로 돌려보냄
        raise SiteClosed("예약 달력이 열리지 않음 (매월 1일 오전 10시 전 등)")
    return html


def available_slots():
    """{ '2026-10-03|A구역01', ... } 오늘 이후 토·일 입실 예약가능 사이트."""
    today = dt.datetime.now(KST).date()
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

    if "--test" in sys.argv:
        now = available_slots()
        print(format_msg(now) if now else "현재 토·일 입실 빈자리 없음")
        send_telegram("✅ 캠핑장 알림 테스트 성공\n현재 토·일 빈자리 " + f"{len(now)}건")
        print("텔레그램 전송 완료")
        return

    state = load_state()
    handle_commands(state)
    kst_now = dt.datetime.now(KST)
    if not state["enabled"]:
        log("알림 꺼짐 - 조회 생략")
    elif kst_now.day == 1 and kst_now.hour < 10:
        log("매월 1일 오전 10시 전 - 사이트 이용 불가 시간이라 조회 생략")
    else:
        try:
            now = available_slots()
        except SiteClosed as e:
            log(f"조회 건너뜀: {e}")  # 기록은 그대로 둬서 10시 이후 중복 알림 방지
        except requests.RequestException as e:
            state["fails"] += 1
            log(f"사이트 접속 실패 {state['fails']}회 연속: {e!r}")
            if state["fails"] == 6:
                send_telegram("⚠️ 캠핑장 사이트에 6회 연속 접속하지 못했습니다. 알림이 늦거나 오지 않을 수 있습니다.")
        else:
            state["fails"] = 0
            prev = set(state["slots"]) if state["slots"] is not None else set()
            new = now - prev
            if new:
                send_telegram(format_msg(new))
                log(f"알림 전송: 새 빈자리 {len(new)}건 (전체 {len(now)}건)")
            else:
                log(f"새 빈자리 없음 (전체 {len(now)}건)")
            state["slots"] = sorted(now)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def load_state():
    s = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    if isinstance(s, list):  # 이전 형식(빈자리 목록만 저장)
        s = {"slots": s}
    return {"enabled": True, "offset": 0, "slots": None, "fails": 0, **s}


def handle_commands(state):
    """텔레그램 명령 처리. 정기 실행 때 한꺼번에 읽으므로 답장은 다음 실행 시각에 온다."""
    api = f"https://api.telegram.org/bot{CONFIG['telegram_token']}"
    requests.post(f"{api}/setMyCommands", json={"commands": [
        {"command": "on", "description": "알림 켜기"},
        {"command": "off", "description": "알림 끄기"},
        {"command": "status", "description": "현재 상태·빈자리"}]}, timeout=20)
    r = requests.get(f"{api}/getUpdates", params={"offset": state["offset"] + 1}, timeout=20)
    for u in r.json().get("result", []):
        state["offset"] = u["update_id"]
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(CONFIG["telegram_chat_id"]):
            continue  # 내 채팅 외 명령 무시
        cmd = msg.get("text", "").strip().split("@")[0]
        if cmd in ("/on", "켜기"):
            state["enabled"] = True
            send_telegram("🔔 알림을 켰습니다.")
        elif cmd in ("/off", "끄기"):
            state["enabled"] = False
            send_telegram("🔕 알림을 껐습니다. 다시 켜려면 /on")
        elif cmd in ("/status", "상태"):
            slots = set(state["slots"] or [])
            head = f"현재 알림: {'켜짐 🔔' if state['enabled'] else '꺼짐 🔕'}\n토·일 빈자리 {len(slots)}건"
            send_telegram(head + ("\n\n" + format_msg(slots) if slots else ""))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"오류: {e!r}")
        raise
