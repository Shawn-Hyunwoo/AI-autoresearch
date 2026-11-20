# 세 CLI(claude, agy, codex)를 비대화형으로 부르는 라우터.
# 안전 수칙을 프롬프트 앞뒤에 붙이고, 실행당 호출 상한·한도 초과 대체를 처리한다. 승격(escalate)은 본 실험 정체 때 제어기가 부른다.
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

PROMPTS = Path(__file__).resolve().parent / "prompts"
P, LIMITS, STATE, CTX = {}, {}, {}, {}
SAVE = None
LOG_DIR = None
TIMEOUT = None    # 호출 한 번의 시간 제한(초). None이면 제한 없음
_lock = threading.Lock()


def setup(providers, limits, state, save, ctx, log_dir, timeout_s=None):
    global P, LIMITS, STATE, CTX, SAVE, LOG_DIR, TIMEOUT
    P, LIMITS, STATE, CTX, SAVE, LOG_DIR, TIMEOUT = providers, limits, state, ctx, save, Path(log_dir), timeout_s
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _run(argv, stdin, cwd, env):
    # 새 프로세스 그룹으로 띄워, 시간 제한에 걸리면 CLI가 띄운 셸과 실험 프로세스까지 함께 끝낸다.
    # stdin이 없으면 DEVNULL: tmux 터미널 입력을 CLI가 가져가지 않게 한다.
    try:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd, env=env,
                             start_new_session=True)
    except FileNotFoundError:   # CLI 자동 업데이트 중에는 실행 파일이 잠깐 없다. 실패로 보고 다시 시도하게 한다
        return 127, "", f"[실행 파일 없음: {argv[0]}]"
    try:
        out, err = p.communicate(stdin, timeout=TIMEOUT)
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
        return -9, out, f"{err}\n[시간 제한 {TIMEOUT}초 초과로 종료]"


def fill(text, ctx):
    # {{key}}를 한 번에 치환한다. 넣은 값 안의 {{...}}는 다시 치환하지 않는다.
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(ctx[m[1]]) if m[1] in ctx else m[0], text)


def _limited(slot):
    # 한도 초과는 모델 단위로 기록한다 (예: claude의 fable 주간 한도가 다 차도 opus는 쓸 수 있다)
    calls, ex = STATE.setdefault("calls", {}), STATE.get("exhausted", [])
    if slot["provider"] in ex or f"{slot['provider']}/{slot['model']}" in ex:
        return True
    return any(k in LIMITS and calls.get(k, 0) >= LIMITS[k] for k in (slot["model"], slot["provider"]))


def resolve(slot):
    # 상한에 닿았거나 한도 초과가 난 모델이면 after_quota → 벤더 fallback 순으로 바꾼다. _lock 안에서 부른다.
    orig = slot
    for _ in range(4):
        if not _limited(slot):
            break
        slot = slot.get("after_quota") or P["vendors"][slot["provider"]]["fallback_on_quota"]
    if _limited(slot):   # 대체 모델도 막혔으면 그 벤더의 승격 사다리에서 막히지 않은 가장 높은 칸 (벤더 하나만 쓸 때 필요)
        steps = [{"provider": slot["provider"], **s} for s in P["vendors"][slot["provider"]].get("escalation_ladder") or []]
        slot = next((s for s in reversed(steps) if not _limited(s)), slot)
    if _limited(slot):
        raise RuntimeError(f"대체 모델까지 모두 한도에 걸림: {slot}")
    if slot is not orig:                                # 완료 알림의 비용 요약에 쓴다
        STATE.setdefault("fallbacks", []).append(f"{orig['provider']}/{orig['model']} → {slot['provider']}/{slot['model']}")
    calls = STATE["calls"]
    for k in (slot["model"], slot["provider"]):
        calls[k] = calls.get(k, 0) + 1
    SAVE()
    return slot


def escalate(slot):
    # 승격 사다리에서 한 칸 위. 사다리 밖의 슬롯은 추론 단계만 한 칸 올린다.
    v = P["vendors"][slot["provider"]]
    steps = [(s["model"], s["effort"]) for s in v.get("escalation_ladder", [])]
    key = (slot["model"], slot["effort"])
    if key in steps and steps.index(key) + 1 < len(steps):
        return {**slot, **v["escalation_ladder"][steps.index(key) + 1]}
    i = v["efforts"].index(slot["effort"])
    return {**slot, "effort": v["efforts"][min(i + 1, len(v["efforts"]) - 1)]}


def call(slot, body, out_file, *, mode, cwd, env, human=False, session=None):
    # mode: "full"(권한 전체 허용) | "readonly". 결과 텍스트를 out_file에 쓰고 돌려준다.
    # session: 여러 번 주고받는 호출이면 호출자가 state에 보관하는 dict. {"history": 대화 기록 파일 경로}로 시작하고
    # 여기서 provider, model, id를 채운다. 같은 모델이면 그 세션을 이어 쓰고, 모델이 바뀌었거나 이어 쓰기가 실패했거나
    # 벤더에 세션 기능이 없으면 새 세션을 열고 대화 기록을 앞에 붙인다.
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    safety = fill((PROMPTS / "_safety.md").read_text(), CTX)
    rules = "\n\n" + (PROMPTS / "_writing_rules.md").read_text() if human else ""
    prompt = f"{safety}\n\n{body}{rules}\n\n{safety}"
    with _lock:
        slot = resolve(slot)
    fails, waited = 0, False
    while True:
        v = P["vendors"][slot["provider"]]
        if slot["effort"] not in v["efforts"]:
            raise RuntimeError(f"{slot['provider']}에 없는 추론 단계: {slot['effort']}")
        s = v.get("session") if session is not None else None
        resume = bool(s and session.get("id") and session.get("provider") == slot["provider"]
                      and session.get("model") == slot["model"])
        msg = prompt
        if session is not None and not resume and Path(session["history"]).exists():
            msg = (f"아래는 이 작업의 앞선 대화 기록이다. 이어서 맨 아래 '이번 지시'에 답한다.\n\n"
                   f"{Path(session['history']).read_text()}\n\n## 이번 지시\n\n{prompt}")
        sid = session["id"] if resume else str(uuid.uuid4())   # 새 세션 번호는 claude만 미리 정해 넘긴다
        cmd = s["resume_cmd"] if resume and s.get("resume_cmd") else v["cmd"]
        argv = [a.replace("{model}", v["aliases"][slot["model"]]).replace("{effort}", slot["effort"])
                .replace("{out}", str(out_file)) for a in cmd]
        if mode == "full":
            argv += v["full_flags"]
        else:
            argv += s["resume_readonly_flags"] if resume and s.get("resume_readonly_flags") else v["readonly_flags"]
        if s:
            argv += [a.replace("{session}", sid) for a in s["resume" if resume else "new"]]
        stdin = None
        if v["prompt_via"] == "stdin":
            argv += v.get("tail", [])
            stdin = msg
        else:
            p = msg
            if len(p.encode()) > 100_000:  # ponytail: 리눅스 인자 하나의 상한(128KB) 때문에 파일로 우회
                pf = out_file.with_suffix(".prompt.md")
                pf.write_text(msg)
                p = f"다음 파일의 지시를 끝까지 읽고 그대로 수행하라: {pf}"
            argv.append(v["prompt_arg"].replace("{prompt}", p))
        tag = f"{out_file.parent.name}_{out_file.stem}.{slot['provider']}-{slot['model']}-{slot['effort']}"
        (LOG_DIR / f"{tag}.prompt.md").write_text(msg)
        if v.get("output") == "file":
            out_file.unlink(missing_ok=True)
        t0 = time.perf_counter()
        rc, out, err = _run(argv, stdin, cwd, env)
        shown = argv if stdin is not None else argv[:-1] + ["-p=<prompt>"]
        (LOG_DIR / f"{tag}.log").write_text(f"argv: {' '.join(shown)}\nrc={rc} {time.perf_counter() - t0:.1f}s\n"
                                            f"--- stdout\n{out}\n--- stderr\n{err}")
        if rc == 127 and not waited:               # 실행 파일 없음: CLI 자동 업데이트 중이면 잠깐 없다. 다시 생길 때까지
            waited = True                          # 최대 5분 기다리고 실패로 세지 않는다
            for _ in range(20):
                time.sleep(15)
                if shutil.which(argv[0]):
                    break
            continue
        text = out_file.read_text() if v.get("output") == "file" and out_file.exists() else out
        new_id = sid
        if s and s["id"] == "jsonl":            # codex --json: 첫 이벤트 줄의 thread_id
            new_id = next((json.loads(ln).get("thread_id") for ln in out.splitlines()
                           if ln.startswith("{") and "thread_id" in ln), None)
        elif s and s["id"] == "json":           # agy --output-format json: {"conversation_id", "response", "status"}
            try:
                j = json.loads(out)
                new_id, text = j.get("conversation_id"), j.get("response", "") if j.get("status") == "SUCCESS" else ""
            except json.JSONDecodeError:
                new_id, text = None, ""
        if rc == 0 and text.strip() and (not s or new_id):
            if v.get("output") != "file":
                out_file.write_text(text)
            if session is not None:
                with _lock:
                    session.update(provider=slot["provider"], model=slot["model"], id=new_id if s else None)
                    with open(session["history"], "a") as f:
                        f.write(f"## 지시\n\n{body}\n\n## 너의 답\n\n{text}\n\n")
                    SAVE()
            return text
        if session is not None:
            session["id"] = None                # 이어 쓰기가 실패했으면 다음 시도는 새 세션 + 대화 기록
        tail = (out[-3000:] + err[-3000:]).lower()
        time.sleep(15)                              # 일시적 문제(CLI 업데이트, 네트워크)가 지나가게 잠깐 기다린다
        with _lock:
            if any(q in tail for q in P["quota_patterns"]):
                STATE.setdefault("exhausted", []).append(f"{slot['provider']}/{slot['model']}")
                slot = resolve(slot)
                continue
            if fails:                              # 두 번째 실패면 멈춘다 (모델을 바꿔도 네트워크·CLI 문제는 안 풀린다)
                raise RuntimeError(f"모델 호출 실패 rc={rc}: {slot}. 로그 {LOG_DIR / (tag + '.log')}")
            fails = 1
            STATE.setdefault("retries", []).append(f"{slot['provider']}/{slot['model']} {out_file.stem}")
            slot = resolve(slot)                   # 같은 슬롯으로 한 번만 더
