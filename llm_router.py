# 세 CLI(claude, agy, codex)를 비대화형으로 부르는 라우터.
# 안전 수칙을 프롬프트 앞뒤에 붙이고, 실행당 호출 상한·한도 초과 대체·승격 사다리를 처리한다.
import os
import re
import signal
import subprocess
import threading
import time
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
    p = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd, env=env,
                         start_new_session=True)
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
    calls = STATE.setdefault("calls", {})
    if slot["provider"] in STATE.get("exhausted", []):
        return True
    return any(k in LIMITS and calls.get(k, 0) >= LIMITS[k] for k in (slot["model"], slot["provider"]))


def resolve(slot):
    # 상한에 닿았거나 한도 초과가 난 벤더면 after_quota → 벤더 fallback 순으로 바꾼다. _lock 안에서 부른다.
    for _ in range(4):
        if not _limited(slot):
            break
        slot = slot.get("after_quota") or P["vendors"][slot["provider"]]["fallback_on_quota"]
    if _limited(slot):
        raise RuntimeError(f"대체 모델까지 모두 한도에 걸림: {slot}")
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


def call(slot, body, out_file, *, mode, cwd, env, human=False):
    # mode: "full"(권한 전체 허용) | "readonly". 결과 텍스트를 out_file에 쓰고 돌려준다.
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    safety = fill((PROMPTS / "_safety.md").read_text(), CTX)
    rules = "\n\n" + (PROMPTS / "_writing_rules.md").read_text() if human else ""
    prompt = f"{safety}\n\n{body}{rules}\n\n{safety}"
    with _lock:
        slot = resolve(slot)
    fails = 0
    while True:
        v = P["vendors"][slot["provider"]]
        if slot["effort"] not in v["efforts"]:
            raise RuntimeError(f"{slot['provider']}에 없는 추론 단계: {slot['effort']}")
        argv = [a.replace("{model}", v["aliases"][slot["model"]]).replace("{effort}", slot["effort"])
                .replace("{out}", str(out_file)) for a in v["cmd"]]
        argv += v["full_flags"] if mode == "full" else v["readonly_flags"]
        stdin = None
        if v["prompt_via"] == "stdin":
            argv += v.get("tail", [])
            stdin = prompt
        else:
            p = prompt
            if len(p.encode()) > 100_000:  # ponytail: 리눅스 인자 하나의 상한(128KB) 때문에 파일로 우회
                pf = out_file.with_suffix(".prompt.md")
                pf.write_text(prompt)
                p = f"다음 파일의 지시를 끝까지 읽고 그대로 수행하라: {pf}"
            argv.append(v["prompt_arg"].replace("{prompt}", p))
        tag = f"{out_file.parent.name}_{out_file.stem}.{slot['provider']}-{slot['model']}-{slot['effort']}"
        (LOG_DIR / f"{tag}.prompt.md").write_text(prompt)
        if v.get("output") == "file":
            out_file.unlink(missing_ok=True)
        t0 = time.perf_counter()
        rc, out, err = _run(argv, stdin, cwd, env)
        shown = argv if stdin is not None else argv[:-1] + ["-p=<prompt>"]
        (LOG_DIR / f"{tag}.log").write_text(f"argv: {' '.join(shown)}\nrc={rc} {time.perf_counter() - t0:.1f}s\n"
                                            f"--- stdout\n{out}\n--- stderr\n{err}")
        text = out_file.read_text() if v.get("output") == "file" and out_file.exists() else out
        if rc == 0 and text.strip():
            if v.get("output") != "file":
                out_file.write_text(text)
            return text
        tail = (out[-3000:] + err[-3000:]).lower()
        with _lock:
            if any(q in tail for q in P["quota_patterns"]):
                STATE.setdefault("exhausted", []).append(slot["provider"])
                slot = resolve(slot)
                continue
            fails += 1
            if fails == 1:
                slot = resolve(slot)               # 같은 슬롯으로 한 번 더
            elif fails == 2:
                slot = resolve(escalate(slot))     # 두 번 연속 실패면 한 칸 승격
            else:
                raise RuntimeError(f"모델 호출 실패 rc={rc}: {slot}. 로그 {LOG_DIR / (tag + '.log')}")
