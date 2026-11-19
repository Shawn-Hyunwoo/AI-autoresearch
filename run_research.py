# 자율 연구 제어기. tmux 안에서 실행한다.
#   python ~/autoresearch/run_research.py --spec ~/autoresearch_runs/<프로젝트>/<run_id>/00_spec.md [--resume]
# 순서: 작업공간 → 1 조사 → 2 아이디어 토론·선별 → 3 파일럿·채점기 승인(트랙 A/B) → 4 트랙 A/B/C → 5 감사·보고 → push 승인
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

import llm_router as llm
import notify

ap = argparse.ArgumentParser()
ap.add_argument("--spec", required=True, help="~/autoresearch_runs/<프로젝트>/<run_id>/00_spec.md")
ap.add_argument("--resume", action="store_true", help="state.json에 기록된 지점부터 이어서 실행")
args = ap.parse_args()

# ---- 경로
TOOL = Path(__file__).resolve().parent
SPEC_PATH = Path(args.spec).expanduser().resolve()
RUN = SPEC_PATH.parent                      # ~/autoresearch_runs/<프로젝트>/<run_id>
RUN_ID = RUN.name
REPO, VENV = RUN / "repo", RUN / "venv"     # 원본 클론(작업 브랜치), 이 실행 전용 venv
OUT = REPO / "research_runs" / RUN_ID       # 산출물. 클론의 .git/info/exclude로 git에서 숨겨 reset·clean을 피한다
EVAL = OUT / "evaluation"                   # prepare.py, metrics.py, test_gate.py, (트랙 B) run_bench.sh, 공개 입력
HIDDEN = RUN / "hidden"                     # 평가 정답. 클론 밖에 두고, 제어기가 채점할 때만 AR_HIDDEN_DIR로 경로를 넘긴다
SELFTEST = OUT / "03_pilot" / "_selftest_hidden"   # builder가 자기 점검에 쓰는 임시 정답 폴더. 호출이 끝나면 지운다
MAIN = OUT / "04_main"
BRANCH = f"autoresearch/{RUN_ID}"
STATE_PATH = RUN / "state.json"             # 클론 밖에 둔다
PY = str(VENV / "bin" / "python")

# ---- 명세와 설정: 벤더를 어떻게 부르는지는 providers.yaml, 무엇을 얼마나 부를지는 명세에만 있다 (겹치지 않음)
SPEC_TEXT = SPEC_PATH.read_text()
CFG = yaml.safe_load(re.search(r"```yaml\n(.*?)\n```", SPEC_TEXT, re.S).group(1))
SRC = Path(CFG["source_repo"]).expanduser().resolve()
PROV_PATH = RUN / "providers.yaml" if (RUN / "providers.yaml").exists() else TOOL / "providers.yaml"   # 실행 폴더 사본 우선
PROV = yaml.safe_load(PROV_PATH.read_text())
TRACKS = CFG["track"] if isinstance(CFG["track"], list) else [t.strip() for t in str(CFG["track"]).split(",")]
STAGES, BUDGET = CFG.get("stages") or {}, CFG.get("budget") or {}
NEED = ["phase1_recon", "phase2_ideation", "crash_repair", "phase5_audit"] \
    + (["phase3_pilot"] if {"A", "B"} & set(TRACKS) else []) + [f"phase4_track_{t.lower()}" for t in TRACKS]
NEED_BUDGET = ["max_crash_repairs", "run_limits"] + (["max_iterations"] if {"A", "B"} & set(TRACKS) else []) \
    + (["seeds"] if "A" in TRACKS else []) + (["bench_rounds"] if "B" in TRACKS else []) \
    + (["max_debate_rounds"] if "C" in TRACKS else [])   # 쓰는 트랙에 필요한 항목만 요구한다
MISSING = [f"stages.{k}" for k in NEED if k not in STAGES] + [f"budget.{k}" for k in NEED_BUDGET if k not in BUDGET]
if MISSING:
    raise SystemExit(f"명세에 빠진 항목: {', '.join(MISSING)} (templates/00_spec_template.md 참고)")
if not STAGES["phase2_ideation"].get("critics"):
    raise SystemExit("stages.phase2_ideation.critics가 비어 있습니다 (diverger마다 critic 한 명과 토론합니다)")


def slots_in(x, path="stages"):
    # 명세 stages 안의 모든 모델 슬롯 (after_quota 포함)
    if isinstance(x, dict):
        if "provider" in x:
            yield path, x
        for k, v in x.items():
            yield from slots_in(v, f"{path}.{k}")
    elif isinstance(x, list):
        for i, v in enumerate(x):
            yield from slots_in(v, f"{path}[{i}]")


# 명세가 쓰는 벤더를 사용자가 쓸 수 있는 벤더로 본다(CLI가 깔려 있어도 구독하지 않은 벤더는 명세에 넣지 않는다).
# 하나뿐이면 분리 규칙을 벤더 대신 모델 단위로 지키고, 한도 초과 대체와 교차 조사도 이 벤더들 안에서만 한다
USED = sorted({s["provider"] for _, s in slots_in(STAGES) if s.get("provider") in PROV["vendors"]})
SOLO = len(USED) == 1
UNIT = "모델" if SOLO else "벤더"
ADAPT = [f"명세가 쓰는 벤더가 {USED[0]} 하나라 교차 검토를 다른 벤더 대신 다른 모델로 했다 (독립성이 약하다)"] if SOLO else []


def kin(s):
    # 분리 규칙의 단위: 벤더. 명세가 쓰는 벤더가 하나뿐이면 모델
    return (s["provider"], s["model"]) if SOLO else s["provider"]


BAD = []   # 오타는 그 단계가 호출될 때가 아니라 시작할 때 잡는다
for where, s in slots_in(STAGES):
    v = PROV["vendors"].get(s["provider"])
    if not v:
        BAD.append(f"{where}: 없는 벤더 '{s['provider']}' (있는 것: {', '.join(PROV['vendors'])})")
    elif s.get("model") not in v["aliases"]:
        BAD.append(f"{where}: {s['provider']}에 없는 모델 '{s.get('model')}' (있는 것: {', '.join(v['aliases'])})")
    elif s.get("effort") not in v["efforts"]:
        BAD.append(f"{where}: {s['provider']}에 없는 추론 단계 '{s.get('effort')}' (있는 것: {', '.join(v['efforts'])})")
for i in range(4):   # 명세가 쓰는 CLI가 있는가. 자동 업데이트 중에는 실행 파일이 잠깐 없으므로 몇 번 다시 본다
    missing = [k for k in USED if not shutil.which(PROV["vendors"][k]["cmd"][0])]
    if not missing or i == 3:
        break
    print(f"CLI를 찾지 못함: {', '.join(missing)}. 15초 뒤 다시 확인 (자동 업데이트 중일 수 있음)", flush=True)
    time.sleep(15)
BAD += [f"{k}: CLI({PROV['vendors'][k]['cmd'][0]})를 찾지 못함. 설치하거나 명세에서 이 벤더를 뺀다" for k in missing]
for k in USED:   # 한도 초과 때 넘어갈 곳이 명세가 쓰지 않는 벤더면, 쓰는 다른 벤더(없으면 자기)의 사다리 맨 위로 바꾼다
    v, fb = PROV["vendors"][k], PROV["vendors"][k].get("fallback_on_quota") or {}
    if fb.get("provider") not in USED:
        alt = next((o for o in USED if o != k), k)
        a = PROV["vendors"][alt]
        v["fallback_on_quota"] = fb = {"provider": alt, **(a.get("escalation_ladder") or [
            {"model": next(iter(a["aliases"])), "effort": a["efforts"][-1]}])[-1]}
        ADAPT.append(f"{k}의 한도 초과 대체를 명세가 쓰지 않는 벤더에서 {alt}/{fb['model']}/{fb['effort']}로 바꿨다")
    f = PROV["vendors"][fb["provider"]]
    if fb.get("model") not in f["aliases"] or fb.get("effort") not in f["efforts"]:
        BAD.append(f"{PROV_PATH.name} vendors.{k}.fallback_on_quota: 없는 모델이나 추론 단계 {fb}")
    if v.get("search") and (v["search"].get("model") not in v["aliases"] or v["search"].get("effort") not in v["efforts"]):
        BAD.append(f"{PROV_PATH.name} vendors.{k}.search: 없는 모델이나 추론 단계 {v['search']}")
if {"A", "C"} & set(TRACKS) and not any(s.get("role") == "lit_search" for s in STAGES["phase1_recon"]["slots"]):
    BAD.append("stages.phase1_recon.slots: 트랙 A·C에는 교차 조사를 합치고 검증할 role: lit_search 슬롯이 필요합니다")
if BAD:
    raise SystemExit(f"명세의 모델 배정과 {PROV_PATH.name}를 확인하세요:\n" + "\n".join(BAD))
AUDIT = STAGES["phase5_audit"]
if AUDIT.get("checker") and kin(AUDIT["checker"]) == kin(AUDIT["reporter"]):
    raise SystemExit(f"phase5_audit의 checker(보고서 검토)와 reporter는 서로 다른 {UNIT}로 둬야 합니다")
LIMITS = BUDGET["run_limits"]
CONTRACT = CFG.get("evaluation_contract") or {}
DIRECTION = CONTRACT.get("direction", "higher_is_better")

# 작성 모델과 반박·감사 모델은 계열(벤더)이 달라야 한다. 한도 초과로 대체된 뒤까지는 보장하지 않는다.
MAIN_KINS = {kin(STAGES[f"phase4_track_{t.lower()}"][r])
             for t, r in (("A", "experimenter"), ("B", "worker"), ("C", "prover")) if t in TRACKS}
if "C" in TRACKS and kin(STAGES["phase4_track_c"]["prover"]) == kin(STAGES["phase4_track_c"]["refuter"]):
    raise SystemExit(f"트랙 C의 prover와 refuter는 서로 다른 {UNIT}로 둬야 합니다")
if MAIN_KINS & {kin(a) for a in STAGES["phase5_audit"]["auditors"]}:
    raise SystemExit(f"Phase 5 감사자는 본 실험과 다른 {UNIT}로 둬야 합니다 (본 실험: {', '.join('/'.join(k) if SOLO else k for k in MAIN_KINS)})")
SPLIT_LOCK = (CFG.get("constraints") or {}).get("split_lock") or []
SPLIT_LOCK = [SPLIT_LOCK] if isinstance(SPLIT_LOCK, str) else SPLIT_LOCK
SECOND_PASS = (CFG.get("constraints") or {}).get("timing_rule") == "second_pass"

# ---- 상태
# 같은 실행은 한 번에 하나만 돈다. 운영체제 파일 잠금이라 프로세스가 끝나거나 죽으면 저절로 풀린다
LOCK = open(RUN / ".lock", "a+")
try:
    fcntl.flock(LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    LOCK.seek(0)
    raise SystemExit(f"이 실행은 이미 다른 곳에서 돌고 있습니다 (PID {LOCK.read().strip() or '?'}). "
                     "그 터미널(tmux 세션)로 가서 보세요. 끝내려면 그 프로세스를 Ctrl-C로 멈춥니다")
LOCK.seek(0)
LOCK.truncate()
LOCK.write(str(os.getpid()))
LOCK.flush()
if STATE_PATH.exists() and not args.resume:
    raise SystemExit(f"이미 시작된 실행입니다 ({STATE_PATH}). 이어서 하려면 --resume")
STATE = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {
    "run_id": RUN_ID, "status": "RUNNING", "phase": None, "done": [], "calls": {}, "exhausted": [],
    "slots": {}, "locks": {}, "base_commit": None, "best_commit": None}
STATE["status"] = "RUNNING"


def save_state():
    STATE_PATH.write_text(json.dumps(STATE, ensure_ascii=False, indent=2))


ENV = dict(os.environ, VIRTUAL_ENV=str(VENV), PATH=f"{VENV / 'bin'}:{os.environ['PATH']}", PYTHONDONTWRITEBYTECODE="1")
ENV.pop("PYTHONHOME", None)
ENV.pop("AR_HIDDEN_DIR", None)
SCORE_ENV = dict(ENV, AR_HIDDEN_DIR=str(HIDDEN))     # 제어기가 prepare.py, test_gate.py, metrics.py를 돌릴 때만
BUILD_ENV = dict(ENV, AR_HIDDEN_DIR=str(SELFTEST))   # builder 에이전트의 자기 점검용
FINAL_ENV = dict(ENV, AR_HIDDEN_DIR=str(HIDDEN / "final"))   # 트랙 A 최종 시험 채점용 (본 실험 동안은 쓰지 않는다)


def visible_locks():
    # 에이전트에게 보여 줄 잠긴 파일 (클론 기준 경로). 숨긴 정답 파일은 이름도 알려 주지 않는다
    return [str(Path(k).relative_to("repo")) for k in STATE["locks"] if k.startswith("repo/")]


TIMEOUT = BUDGET.get("call_timeout_min")
for a in ADAPT:
    print(f"[조정] {a}", flush=True)
llm.setup(PROV, LIMITS, STATE, save_state, {
    "out_dir": OUT, "eval_dir": EVAL, "branch": BRANCH, "venv": VENV,
    "locked": ", ".join(visible_locks()) or "아직 없음"}, RUN / "llm_logs", TIMEOUT * 60 if TIMEOUT else None)


# ---- 공통 도구
class Stop(Exception):
    # 오류가 아닌 멈춤: 파일럿 신호 없음, 트랙 C 기각, push 관문 등.
    # by_user=True는 사람이 tmux에서 직접 고른 멈춤(q, push 거절)이라 Slack으로 다시 알리지 않는다
    def __init__(self, msg, by_user=False):
        super().__init__(msg)
        self.by_user = by_user


def say(msg):
    print(msg, flush=True)
    notify.send(msg)


def ask(prompt):
    # 관문 입력. readline을 켜면 방향키·Home·End·Delete가 키 코드로 들어가지 않고 줄 편집으로 동작한다.
    # 그래도 키 코드(ESC로 시작: 방향키, Ctrl/Shift+방향키, PgUp/PgDn, F키, Alt+키 등)나 제어 문자가 남으면 다시 묻는다.
    try:
        import readline  # noqa: F401
    except ImportError:
        pass
    while True:
        try:
            a = input(prompt)
        except EOFError:
            raise Stop("입력을 받을 수 없음(tmux 밖 실행). tmux에서 --resume으로 이어서 실행")
        except KeyboardInterrupt:
            raise Stop("관문에서 Ctrl-C로 멈춤. 이어 하려면 --resume", by_user=True)
        a = a.replace("\x1b[200~", "").replace("\x1b[201~", "").replace("\t", " ").strip()   # 붙여넣기 표시, 탭
        if any(ord(c) < 32 or ord(c) == 127 or 0x80 <= ord(c) < 0xA0 for c in a):
            print("방향키 같은 키 코드가 섞여 있어 받지 않았습니다. 다시 입력하세요. "
                  "(화면 스크롤은 Ctrl-b 다음 [, 나올 때는 q)", flush=True)
            continue
        return a


def sh(cmd, cwd=REPO, check=True, env=None):
    r = subprocess.run(cmd, cwd=cwd, env=env or ENV, capture_output=True, text=True, shell=isinstance(cmd, str),
                       stdin=subprocess.DEVNULL)    # 관문 입력(tmux)을 자식 프로세스가 가져가지 않게
    if check and r.returncode:
        raise RuntimeError(f"명령 실패 rc={r.returncode}: {cmd}\n{r.stderr[-2000:]}")
    return r


def git(*a, cwd=REPO):
    return sh(["git", *a], cwd=cwd).stdout.strip()


def read(p, default=""):
    p = Path(p)
    return p.read_text() if p.exists() else default


def tail(p, n=40):
    return "\n".join(read(p, "없음").splitlines()[-n:])


def prompt(name, **kw):
    return llm.fill((TOOL / "prompts" / name).read_text(), kw)


def last_json(text):
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return None


def block_json(text):
    m = re.search(r"```json\s*(.*?)```", text, re.S)
    return json.loads(m.group(1) if m else text[text.index("{"): text.rindex("}") + 1])


def sha256(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def slot(key, default):
    # 승격된 슬롯은 state에 남겨 --resume 뒤에도 이어 쓴다
    return STATE["slots"].get(key, default)


def agent(key, default, body, out_file, mode, human=False, env=None, session=None):
    return llm.call(slot(key, default), body, out_file, mode=mode, cwd=REPO, env=env or ENV, human=human,
                    session=session)


SELECT_SCHEMA = ('```json\n{"sota_baselines": [{"id": "BASE_1", "name": "...", "kind": "classical|quantum", "source": "..."}],\n'
                 ' "selected_candidates": [{"id": "D1_A", "title": "...", "mechanism": "...", "quantum_rationale": "(트랙 A만)", '
                 '"pilot_setup": "...", "rank_reason": "..."}],\n "dropped_candidates": [{"id": "D2_A", "reason": "..."}]}\n```')
VERDICT_SCHEMA = '출력 마지막 줄에 JSON 한 줄: {"logic_sound": true, "scope_realistic": true, "non_trivial": true, "fatal_flaws": []}'
CRITIC_SCHEMA = ('```json\n{"verdicts": [{"id": "D1_A", "verdict": "accept|revise|reject", "reason": "...", '
                 '"request": "revise일 때만"}]}\n```')


JUDGE_RE = re.compile(r"\n*```json\s*(\{[^`]*?\"fatal_judgments\"[^`]*?\})\s*```\s*", re.S)   # 보고서 끝의 판단 블록
FIX_SCHEMA = '```json\n{"fixes": [{"find": "보고서 원문 그대로", "replace": "고친 문장", "why": "근거 파일과 값"}]}\n```'


def parse_fixes(text):
    fx = block_json(text).get("fixes")
    if not isinstance(fx, list) or any(not isinstance(f, dict) or not isinstance(f.get("find"), str)
                                       or not isinstance(f.get("replace"), str) for f in fx):
        raise ValueError("fixes 형식이 틀림")
    return fx


def judgments(j):
    # {"fatal_judgments": [...]} → {치명 지적 번호: (인정|반박, 한 줄)}
    return {int(x["n"]): (x["verdict"], str(x.get("note", ""))) for x in j["fatal_judgments"]
            if x.get("verdict") in ("인정", "반박")}


def parse_critique(final):
    # 비평자 판정. 마지막 회차에도 revise를 내면 결론을 못 낸 것으로 보고 reject로 바꾼다
    def parse(text):
        vs = block_json(text).get("verdicts")
        if not isinstance(vs, list) or not vs or any(
                not isinstance(v, dict) or v.get("verdict") not in ("accept", "revise", "reject") for v in vs):
            raise ValueError("verdicts 형식이 틀림")
        for v in vs:
            if final and v["verdict"] == "revise":
                v["verdict"], v["reason"] = "reject", f"(마지막 회차에도 보완 요청이라 탈락으로 처리) {v.get('reason', '')}"
        return vs
    return parse


def parse_selection(text):
    j = block_json(text)
    if not isinstance(j.get("selected_candidates"), list):
        raise ValueError("selected_candidates 배열이 없음")
    return j


def parse_verdict(text):
    j = last_json(text)
    if not j or "logic_sound" not in j:
        raise ValueError("판정 JSON 줄이 없음")
    return j


def agent_json(key, default, body, out_file, mode, parse, schema, session=None):
    # 정해진 JSON 형식이 틀리면 같은 모델에게 형식만 고쳐 다시 내라고 한 번 요청한다. 두 번째도 틀리면 ValueError
    text = agent(key, default, body, out_file, mode, session=session)
    try:
        return text, parse(text)
    except ValueError:
        fixed = agent(key, default, prompt("json_fix.md", schema=schema, text=text[-8000:]),
                      Path(out_file).with_name(Path(out_file).stem + "_jsonfix.md"), "readonly", session=session)
        return text, parse(fixed)


def parallel(jobs):
    # jobs: [(slot, body, out_file, mode, human)]. 같은 단계의 독립 호출을 동시에 돌린다.
    with ThreadPoolExecutor(max(1, len(jobs))) as ex:
        futs = [ex.submit(llm.call, s, b, o, mode=m, cwd=REPO, env=ENV, human=h) for s, b, o, m, h in jobs]
        return [f.result() for f in futs]


def base_python(env):
    p = Path(env).expanduser()
    if p.is_file():
        return str(p)
    if (p / "bin" / "python").exists():
        return str(p / "bin" / "python")
    conda = os.environ.get("CONDA_EXE") or shutil.which("conda")
    if not conda:
        raise RuntimeError(f"base_env '{env}'를 찾지 못함 (conda 없음). python 경로를 적어 주세요")
    return subprocess.run([conda, "run", "-n", env, "python", "-c", "import sys; print(sys.executable)"],
                          capture_output=True, text=True, check=True).stdout.strip()


def verify_locks():
    for rel, h in STATE["locks"].items():
        p = RUN / rel
        if not p.exists() or sha256(p) != h:
            raise RuntimeError(f"잠긴 파일이 바뀜: {rel}. 채점 조작 가능성이 있어 멈춤")


def lock_eval():
    files = [p for p in EVAL.rglob("*") if p.is_file()] + [REPO / s for s in SPLIT_LOCK] \
        + [p for p in HIDDEN.rglob("*") if p.is_file()]
    for p in files:
        p.chmod(0o444)
        STATE["locks"][str(p.relative_to(RUN))] = sha256(p)
    (OUT / "lock.json").write_text(json.dumps({k: STATE["locks"][f"repo/{k}"] for k in visible_locks()}, indent=2))
    llm.CTX["locked"] = ", ".join(visible_locks())
    save_state()


def repair(work, cmd, log, n):
    agent("crash_repair", STAGES["crash_repair"], prompt("crash_repair.md", cmd=cmd, log=log[-6000:], work=work),
          Path(work) / f"repair_{n:02d}.md", "full")


def run_once(script, res, seed=0):
    # build_cmd → script 실행 한 번 (second_pass면 두 번 돌려 두 번째 시간만 쓴다). 반환: (종료 코드, 로그, 시간)
    env = dict(ENV, AR_SEED=str(seed))
    b = sh(CFG["build_cmd"], check=False, env=env) if CFG.get("build_cmd") else None
    if b is not None and b.returncode:
        return b.returncode, f"[build_cmd 실패]\n{b.stdout}{b.stderr}", None
    rc, log, wall = 0, "", None
    for _ in range(2 if SECOND_PASS else 1):
        shutil.rmtree(res, ignore_errors=True)
        res.mkdir(parents=True)
        t0 = time.perf_counter()
        r = sh(["bash", str(script), str(res)], check=False, env=env)
        wall, rc, log = time.perf_counter() - t0, r.returncode, r.stdout + r.stderr
        if rc:
            break
    return rc, log, wall


def score(res, wall, regime=None, final=False):
    # metrics.py는 제어기만 돌리고, 이때만 숨긴 정답 경로를 넘긴다. regime은 prepare.py가 미리 만든 입력 변형의 이름
    # final=True면 최종 시험 정답으로 채점한다. 입력을 바꿔 끼운 동안이라 잠금 확인은 부르는 쪽이 앞뒤로 한다
    # ponytail: 에이전트와 같은 리눅스 사용자라 정답 폴더를 일부러 찾아 읽는 것까지는 막지 못한다 (막으려면 별도 사용자나 컨테이너)
    if not final:
        verify_locks()
    r = sh([PY, str(EVAL / "metrics.py"), str(res)] + ([regime] if regime else []), check=False,
           env=FINAL_ENV if final else SCORE_ENV)
    s = last_json(r.stdout) or {"guard_pass": False, "primary_metric": None,
                                 "guard_reason": f"metrics.py 출력 없음: {r.stderr[-300:]}"}
    s.setdefault("wall_time_sec", wall)
    return s


def run_case(work, script, seed=0, by_case=False):
    # script를 실행해 work/result에 결과를 만들고 채점한다. 비정상 종료(crash)면 수리 후 다시 돈다.
    # by_case=True(트랙 A 본 실험)면 result 아래 경우별 폴더(<모델 id> 또는 <모델 id>@<regime>)를 하나씩 채점한다.
    res = work / "result"
    for attempt in range(BUDGET["max_crash_repairs"] + 1):
        rc, log, wall = run_once(script, res, seed)
        work.mkdir(parents=True, exist_ok=True)
        (work / "run.log").write_text(log)
        if rc == 0:
            break
        if attempt == BUDGET["max_crash_repairs"]:
            return {"status": "crash", "guard_pass": False, "primary_metric": None, "wall_time_sec": wall,
                    "guard_reason": log[-300:]}
        repair(work, f"AR_SEED={seed} bash {script} {res}", log, attempt + 1)
    if by_case:
        cases = {c.name: score(c, wall, c.name.split("@", 1)[1] if "@" in c.name else None)
                 for c in sorted(p for p in res.iterdir() if p.is_dir())}
        s = {"status": "ok", "wall_time_sec": wall, "cases": cases or {"(경우 폴더 없음)": {
            "guard_pass": False, "primary_metric": None, "guard_reason": "result 아래에 경우별 폴더가 없음"}}}
    else:
        s = score(res, wall)
        s["status"] = "ok"
    (work / "score.json").write_text(json.dumps(s, ensure_ascii=False, indent=2))
    return s


def nums(rows, key="primary_metric"):
    return [r[key] for r in rows if isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)]


def case_table(cases):
    # 경우별 비교표 (마크다운). 누적표에는 그 값을 낸 시도 번호가 붙는다
    return (f"| 경우 | {CONTRACT.get('metric', 'metric')} 평균 | 표준오차 | 시드 수 | guard | 시도 |\n|---|---|---|---|---|---|\n"
            + "".join(f"| {k} | {v['primary_metric']} | {v['spread']} | {v['n_seeds']} | {v['guard_pass']} | "
                      f"{v.get('attempt', '')} |\n" for k, v in sorted(cases.items())))


def summarize(per):
    # 경우별 시드 결과 → 시드 평균, 평균의 표준오차. 모든 시드에서 guard를 통과해야 guard_pass
    cases = {}
    for name, runs in per.items():
        vals = nums(runs)
        cases[name] = {"guard_pass": all(r.get("guard_pass") for r in runs) and len(vals) == BUDGET["seeds"],
                       "primary_metric": statistics.mean(vals) if vals else None,
                       "spread": statistics.stdev(vals) / len(vals) ** 0.5 if len(vals) > 1 else None,
                       "n_seeds": len(vals), "wall_time_sec": statistics.mean(nums(runs, "wall_time_sec") or [0]),
                       "guard_reason": next((r.get("guard_reason", "") for r in runs if not r.get("guard_pass")), "")}
    return cases


def attempt_a(work):
    # 트랙 A 시도 1회: run.sh를 시드(AR_SEED)마다 돌리고, 경우(모델 × regime)마다 제어기가 채점해
    # 시드 평균과 평균의 표준오차를 낸다. 모든 경우가 모든 시드에서 guard를 통과해야 채택한다
    script = work / "run.sh"
    if not script.exists():
        return {"status": "crash", "guard_pass": False, "cases": {}, "guard_reason": "run.sh 없음"}
    per = {}
    for sd in range(BUDGET["seeds"]):
        s = run_case(work / f"seed_{sd}", script, sd, by_case=True)
        if s["status"] == "crash":
            return {**s, "cases": {}}
        for name, sc in s["cases"].items():
            per.setdefault(name, []).append(sc)
    cases = summarize(per)
    (work / "comparison.md").write_text(case_table(cases))   # 제어기가 채점한 이번 시도의 경우별 비교표
    out = {"status": "ok", "cases": cases, "guard_pass": all(v["guard_pass"] for v in cases.values()),
           "guard_reason": next((f"{k}: {v['guard_reason']}" for k, v in cases.items() if not v["guard_pass"]), "")}
    (work / "score.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    return out


def bench_pair(work, commits, rounds):
    # 커밋 둘을 라운드마다 순서를 바꿔 번갈아 잰다(순서 효과를 줄인다). 반환: {이름: [채점 결과, ...]}
    runs, order = {k: [] for k in commits}, list(commits)
    for r in range(rounds):
        for label in (order if r % 2 == 0 else order[::-1]):
            git("checkout", "-q", "--detach", commits[label])
            d = work / f"round_{r + 1}_{label}"
            rc, log, wall = run_once(EVAL / "run_bench.sh", d / "result")
            sc = score(d / "result", wall) if rc == 0 else {
                "guard_pass": False, "primary_metric": None, "guard_reason": f"측정 중 비정상 종료: {log[-200:]}"}
            (d / "score.json").write_text(json.dumps(sc, ensure_ascii=False, indent=2))
            runs[label].append(sc)
    git("checkout", "-q", BRANCH)                       # 작업 브랜치는 마지막 커밋(트랙 B 시도면 후보)을 가리킨다
    return runs


def attempt_b(work):
    # 트랙 B 시도 1회: (1) 한 번 실행해 오류와 guard 확인 (crash면 수리) (2) 후보를 작업 브랜치에 임시 커밋
    # (3) 후보와 현재 최고 커밋을 번갈아 bench_rounds번씩 재고 중앙값을 비교한다 (라운드마다 순서를 바꿔 순서 효과를 줄인다)
    s = run_case(work / "check", EVAL / "run_bench.sh")
    if s["status"] == "crash" or not s.get("guard_pass"):
        return s
    git("add", "-A")
    git("commit", "--allow-empty", "-q", "-m", "candidate (측정 중)")
    runs = bench_pair(work, {"cand": git("rev-parse", "HEAD"), "best": STATE["best_commit"]}, BUDGET["bench_rounds"])
    cv, bv = nums(runs["cand"]), nums(runs["best"])
    cm = statistics.median(cv) if cv else None
    bm = statistics.median(bv) if bv else None
    out = {"status": "ok", "guard_pass": all(x.get("guard_pass") for x in runs["cand"]) and len(cv) == len(runs["cand"]),
           "primary_metric": cm, "best_median": bm, "delta_pct": pct(cm, bm),
           "spread": (max(cv) - min(cv)) / 2 if cv else None,
           "wall_time_sec": statistics.median(nums(runs["cand"], "wall_time_sec") or [0]),
           "guard_reason": next((x.get("guard_reason", "") for x in runs["cand"] if not x.get("guard_pass")), "")}
    (work / "score.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    return out


def pct(m, best):
    # 개선 폭(%). 양수가 좋아진 쪽이다.
    if m is None or best in (None, 0):
        return None
    d = (m - best) / abs(best) * 100
    return d if DIRECTION == "higher_is_better" else -d


# ---- 작업 공간: 원본 클론 + 작업 브랜치 + 입력 복사 + venv
def phase_workspace():
    if not REPO.exists():
        sh(["git", "clone", "--branch", CFG.get("base_branch", "main"), str(SRC), str(REPO)], cwd=RUN)
        git("remote", "remove", "origin")
        git("checkout", "-b", BRANCH)
        with open(REPO / ".git" / "info" / "exclude", "a") as f:
            f.write(f"\n/research_runs/{RUN_ID}/\n")
        for rel in CFG.get("copy_inputs") or []:        # 원본에서 git-ignored 상태인 입력을 복사 (링크하지 않음)
            s, d = SRC / rel, REPO / rel
            if s.is_dir():
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, d)
    STATE["base_commit"] = STATE["base_commit"] or git("rev-parse", "HEAD")
    STATE["best_commit"] = STATE["best_commit"] or STATE["base_commit"]
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SPEC_PATH, OUT / "00_spec.md")
    shutil.copy2(PROV_PATH, OUT / "providers.yaml")   # 이번 실행 때의 벤더 호출 규격 기록
    if not VENV.exists():
        sh([base_python(CFG["base_env"]), "-m", "venv", "--system-site-packages", str(VENV)], cwd=RUN)
        for c in CFG.get("setup_cmds") or []:
            sh(c)
    v = sh([PY, "-m", "pip", "list", "--format=freeze"], check=False).stdout
    (OUT / "env.txt").write_text("".join(f"조정: {a}\n" for a in ADAPT)
                                 + f"{sh([PY, '--version']).stdout}base_commit {STATE['base_commit']}\n\n{v}")


# ---- Phase 1: 저장소 내부 자산 + 선행연구(SoTA 기준선, 알려진 결과, 이미 알려진 접근).
# 트랙 A·C는 search가 있는 벤더들이 같은 주제를 따로 조사하고(교차 조사), lit_search 슬롯의 모델이 합치고 검증한다.
# 트랙 B만이면 lit_search 한 번으로 끝낸다
def references():
    # 명세 references: 사용자가 준 참고 자료. 경로면 클론의 .ar_refs/에 복사하고(git에서 제외되어 push되지 않음),
    # 경로가 아니면(URL, 논문 제목 등) 글 그대로 넘긴다
    refs, out = CFG.get("references") or [], []
    ex = REPO / ".git" / "info" / "exclude"
    if refs and "/.ar_refs/" not in ex.read_text():
        with open(ex, "a") as f:
            f.write("/.ar_refs/\n")
    for r in refs:
        p = Path(str(r)).expanduser()
        p = p if p.is_absolute() else SRC / p
        if p.exists():
            d = REPO / ".ar_refs" / p.name
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(p, d, dirs_exist_ok=True) if p.is_dir() else shutil.copy2(p, d)
            out.append(f"- {d.relative_to(REPO)} (사용자 자료. 먼저 읽는다)")
        else:
            out.append(f"- {r}")
    return "\n".join(out) or "없음"


def check_citations(text):
    # 글에 나온 arXiv 번호와 DOI를 arXiv API와 Crossref에서 직접 조회한다(모델 없이). 결과는 마크다운 표
    ids = sorted({m for m in re.findall(r"(?<![\d.])(\d{4}\.\d{4,5})(?:v\d+)?(?!\.?\d)", text) if 1 <= int(m[2:4]) <= 12})
    dois = sorted({d.rstrip(".,;:)]}*_") for d in re.findall(r"\b10\.\d{4,9}/[^\s\"'<>|`]+", text)})
    head, rows = {"User-Agent": "AI-autoresearch citation check"}, []
    cell = lambda x: " ".join(str(x).split()).replace("|", "/")
    for k in range(0, len(ids), 50):
        chunk = ids[k:k + 50]
        try:
            xml = urllib.request.urlopen(urllib.request.Request(
                f"https://export.arxiv.org/api/query?id_list={','.join(chunk)}&max_results={len(chunk)}", headers=head),
                timeout=30).read()
            ns, found = {"a": "http://www.w3.org/2005/Atom"}, {}
            for e in ET.fromstring(xml).findall("a:entry", ns):
                m = re.search(r"abs/(.+?)(v\d+)?$", e.findtext("a:id", "", ns))
                if m:
                    found[m[1]] = (cell(e.findtext("a:title", "", ns)), cell(e.findtext("a:author/a:name", "", ns)),
                                   e.findtext("a:published", "", ns)[:4])
            rows += [f"| arXiv:{x} | 있음 | {' | '.join(found[x])} |" if x in found else f"| arXiv:{x} | **없음** | | | |"
                     for x in chunk]
        except Exception as e:
            rows += [f"| arXiv:{x} | 조회 실패 ({type(e).__name__}) | | | |" for x in chunk]
    for d in dois:
        try:
            m = json.loads(urllib.request.urlopen(urllib.request.Request(
                f"https://api.crossref.org/works/{urllib.parse.quote(d)}", headers=head), timeout=20).read())["message"]
            rows.append(f"| doi:{d} | 있음 | {cell((m.get('title') or [''])[0])} | "
                        f"{cell((m.get('author') or [{}])[0].get('family', ''))} | "
                        f"{((m.get('issued') or {}).get('date-parts') or [['']])[0][0]} |")
        except urllib.error.HTTPError as e:
            rows.append(f"| doi:{d} | {'**없음**' if e.code == 404 else f'조회 실패 ({e.code})'} | | | |")
        except Exception as e:
            rows.append(f"| doi:{d} | 조회 실패 ({type(e).__name__}) | | | |")
    if not rows:
        return "글에 arXiv 번호나 DOI가 없음"
    return "| 인용 | 조회 결과 | 제목 | 첫 저자 | 연도 |\n|---|---|---|---|---|\n" + "\n".join(rows)


def phase_recon():
    st, refs = STAGES["phase1_recon"], references()
    deep = bool({"A", "C"} & set(TRACKS))
    plain = [s for s in st["slots"] if not (deep and s["role"] == "lit_search")]
    finders = (st.get("searchers") or [{"provider": k, **v["search"]} for k, v in PROV["vendors"].items()
                                       if v.get("search") and k in USED]) if deep else []
    texts = parallel([(s, prompt(f"phase1_{s['role']}.md", spec=SPEC_TEXT, references=refs),
                       OUT / "01_recon" / f"recon_{i:02d}.md", "readonly", True) for i, s in enumerate(plain, 1)]
                     + [(s, prompt("phase1_search.md", n=i, track=",".join(TRACKS), spec=SPEC_TEXT, references=refs),
                         OUT / "01_recon" / f"search_{i:02d}.md", "readonly", True) for i, s in enumerate(finders, 1)])
    parts = [f"# {s['role']} ({s['provider']}/{s['model']}/{s['effort']})\n\n{t}" for s, t in zip(plain, texts)]
    if deep:
        synth = next(s for s in st["slots"] if s["role"] == "lit_search")
        who = ", ".join(f"{s['provider']}/{s['model']}" for s in finders)
        reports = "\n\n".join(f"## 조사자 {i} ({s['provider']}/{s['model']})\n\n{t}"
                               for i, (s, t) in enumerate(zip(finders, texts[len(plain):]), 1))
        lit = agent("lit_search", synth, prompt("phase1_lit_synth.md", track=",".join(TRACKS), spec=SPEC_TEXT,
                                                references=refs, who=who, reports=reports,
                                                citations=check_citations(reports)),
                    OUT / "01_recon" / "lit_synth.md", "readonly", human=True)
        parts.append(f"# lit_search (교차 조사: {who} / 정리·검증: {synth['provider']}/{synth['model']}/{synth['effort']})"
                     f"\n\n{lit}\n\n## 인용 확인 (제어기가 arXiv와 Crossref에서 직접 조회)\n\n{check_citations(lit)}")
    (OUT / "01_recon.md").write_text("\n\n".join(parts))


# ---- Phase 2: 아이디어 토론 → 선별. diverger마다 critic 한 명과 1:1로 최대 max_ideation_rounds회 주고받고,
# 보완 요청이 남지 않으면 그 토론은 끝난다. 통과 후보가 없으면 탈락 사유를 알리고 멈춘다
def pair_critics(divs, crits):
    # diverger마다 critic 한 명. 다른 벤더(명세가 쓰는 벤더가 하나뿐이면 다른 모델) critic을 우선하고, 덜 배정된 critic을 고른다
    used, out = [0] * len(crits), []
    for dv in divs:
        js = [j for j, c in enumerate(crits) if kin(c) != kin(dv)] or list(range(len(crits)))
        j = min(js, key=lambda j: (used[j], j))
        used[j] += 1
        out.append(j)
    return out


def debate_turn(t, r, R, st, recon, d):
    # 토론 하나의 r회차: diverger 차례 → critic 차례. 비평자 판정 목록을 돌려준다 (형식을 끝내 못 맞추면 None)
    i, final = t["n"], r == R
    dslot, cslot = st["divergers"][i - 1], st["critics"][t["critic"]]
    rule = "이번이 마지막 판정이다. revise는 쓸 수 없다. 완벽하지 않아도 지금 내용으로 accept나 reject를 정한다." if final else ""
    if r == 1:
        body = prompt("phase2_diverge.md", n=i, track=",".join(TRACKS), lens=dslot.get("lens", "자유"), spec=SPEC_TEXT,
                      recon=recon, max_rounds=R)
    else:
        body = prompt("phase2_revise.md", round=r, max_rounds=R, critique=read(d / f"t{i:02d}_r{r - 1}_critic.md"),
                      last=" 이번 답 뒤의 판정이 마지막이고, 그때는 보완 요청 없이 채택이나 탈락으로 끝난다." if final else "")
    reply = agent(f"ideation_d{i}", dslot, body, d / f"t{i:02d}_r{r}_diverger.md", "readonly", session=t["dsess"])
    if r == 1:
        cbody = prompt("phase2_critic.md", spec=SPEC_TEXT, recon=recon, ideas=reply, max_rounds=R, final_rule=rule)
    else:
        cbody = prompt("phase2_recritic.md", round=r, max_rounds=R, reply=reply, final_rule=rule,
                       open_ids=", ".join(k for k, v in t["verdicts"].items() if v["verdict"] == "revise"))
    try:
        return agent_json(f"ideation_c{i}", cslot, cbody, d / f"t{i:02d}_r{r}_critic.md", "readonly",
                          parse_critique(final), CRITIC_SCHEMA, session=t["csess"])[1]
    except ValueError:
        return None


def debate_log(t, d, rounds):
    # 선별자에게 넘길 토론 기록: 회차별 발산자 답과 비평자 판정, 후보별 최종 판정
    st, n = STAGES["phase2_ideation"], t["n"]
    dv, cr = st["divergers"][n - 1], st["critics"][t["critic"]]
    parts = [f"## 토론 {n}: diverger {dv['provider']}/{dv['model']} ↔ critic {cr['provider']}/{cr['model']}"]
    for r in range(1, rounds + 1):
        dfile, cfile = d / f"t{n:02d}_r{r}_diverger.md", d / f"t{n:02d}_r{r}_critic.md"
        if dfile.exists():
            parts += [f"### {r}회차 발산자\n{read(dfile)}", f"### {r}회차 비평자\n{read(cfile, '없음')}"]
    parts.append("### 최종 판정\n" + ("".join(f"- {k}: {v['verdict']} ({v['round']}회차) {v.get('reason', '')}\n"
                                          for k, v in t["verdicts"].items()) or "판정 JSON 없음. 기록을 보고 판단한다\n"))
    return "\n\n".join(parts)


def phase_ideation():
    st, recon, d = STAGES["phase2_ideation"], read(OUT / "01_recon.md"), OUT / "02_ideation"
    R = int(BUDGET.get("max_ideation_rounds", 1))
    I = STATE.setdefault("ideation", {"round": 0, "threads": [
        {"n": i, "critic": j, "open": True, "verdicts": {}, "dsess": {"history": str(d / f"t{i:02d}_diverger.history.md")},
         "csess": {"history": str(d / f"t{i:02d}_critic.history.md")}}
        for i, j in enumerate(pair_critics(st["divergers"], st["critics"]), 1)]})
    while I["round"] < R and any(t["open"] for t in I["threads"]):
        r = I["round"] + 1
        opened = [t for t in I["threads"] if t["open"]]
        print(f"  토론 {r}/{R}회차: {len(opened)}개 진행", flush=True)
        with ThreadPoolExecutor(len(opened)) as ex:
            res = list(ex.map(lambda t: debate_turn(t, r, R, st, recon, d), opened))
        for t, vs in zip(opened, res):               # state는 모든 토론의 차례가 끝난 뒤 여기서만 고친다
            for v in vs or []:
                t["verdicts"][str(v.get("id"))] = {**v, "round": r}
            t["open"] = bool(vs) and any(v["verdict"] == "revise" for v in vs)
        I["round"] = r
        save_state()
    logs = "\n\n".join(debate_log(t, d, I["round"]) for t in I["threads"])
    (d / "debates.md").write_text(logs)
    _, sel = agent_json("selector", st["selector"], prompt("phase2_select.md", spec=SPEC_TEXT, recon=recon, debates=logs),
                        d / "selector.md", "readonly", parse_selection, SELECT_SCHEMA)
    (d / "02_selected.json").write_text(json.dumps(sel, ensure_ascii=False, indent=2))
    if not sel.get("selected_candidates"):   # 토론을 처음부터 다시 하지 않는다. 사유를 보고 명세를 고쳐 새로 실행한다
        raise Stop("통과 후보 0개. 탈락 사유:\n" + "\n".join(
            f"- {c.get('id')}: {c.get('reason', '')}" for c in sel.get("dropped_candidates") or []))


# ---- Phase 3: 평가 하네스 작성 → 자동 점검 → 파일럿 (트랙 A/B)
def harness_check():
    for attempt in range(BUDGET["max_crash_repairs"] + 1):
        shutil.rmtree(HIDDEN, ignore_errors=True)       # 정답은 매번 prepare.py로 새로 만든다
        HIDDEN.mkdir(parents=True)
        r1 = sh([PY, str(EVAL / "prepare.py")], cwd=EVAL, check=False, env=SCORE_ENV)
        r2 = sh([PY, str(EVAL / "test_gate.py")], cwd=EVAL, check=False, env=SCORE_ENV) if r1.returncode == 0 else r1
        log = f"[prepare.py rc={r1.returncode}]\n{r1.stdout}{r1.stderr}\n[test_gate.py rc={r2.returncode}]\n{r2.stdout}{r2.stderr}"
        miss = []
        if "A" in TRACKS and r2.returncode == 0:        # 트랙 A는 regime마다 최종 시험 입력과 정답도 있어야 한다
            regs = sorted(p.name for p in (EVAL / "inputs").glob("*") if p.is_dir()) or ["main"]
            miss = [name for r in regs for name, d in ((f"{EVAL}/inputs/{r}", EVAL / "inputs" / r),
                                                       (f"$AR_HIDDEN_DIR/final/inputs/{r}", HIDDEN / "final" / "inputs" / r),
                                                       (f"$AR_HIDDEN_DIR/final/{r}", HIDDEN / "final" / r)) if not d.is_dir()]
            if miss:
                log += "\n[제어기 점검] 트랙 A 입력 또는 최종 시험 폴더 없음: " + ", ".join(miss)
        (OUT / "03_pilot" / "test_gate.log").write_text(log)
        if r1.returncode == 0 and r2.returncode == 0 and not miss:
            return
        if attempt < BUDGET["max_crash_repairs"]:
            repair(OUT / "03_pilot", f"cd {EVAL} && python prepare.py && python test_gate.py", log, attempt + 1)
    raise RuntimeError("prepare.py 또는 test_gate.py 3단계 점검을 통과하지 못함")


def pilot_runs():
    pilot = OUT / "03_pilot"
    if "A" in TRACKS:
        cases = {p.name: p / "run.sh" for p in sorted(pilot.iterdir()) if (p / "run.sh").exists()}
    else:  # ponytail: 트랙 B 파일럿은 현재 코드 측정만. 최적화 후보는 Phase 4 루프에서 시도한다
        cases = {"current_code": EVAL / "run_bench.sh"}
    rows = {name: run_case(pilot / name, script) for name, script in cases.items()}
    table = "파일럿: 최소 규모 동작 확인 (성능 순위가 아님)\n\n" \
        "| case | status | guard_pass | primary_metric | wall_time_sec |\n|---|---|---|---|---|\n" + "".join(
        f"| {k} | {v['status']} | {v.get('guard_pass')} | {v.get('primary_metric')} | {v.get('wall_time_sec')} |\n"
        for k, v in rows.items())
    (pilot / "pilot_table.md").write_text(table)
    return rows, table


def phase_pilot():
    agent("builder", STAGES["phase3_pilot"]["builder"], prompt(
        "phase3_build.md", track=",".join(TRACKS), spec=SPEC_TEXT, recon=read(OUT / "01_recon.md"),
        selected=read(OUT / "02_ideation" / "02_selected.json"), eval_dir=EVAL, pilot_dir=OUT / "03_pilot"),
        OUT / "03_pilot" / "builder.md", "full", env=BUILD_ENV)
    shutil.rmtree(SELFTEST, ignore_errors=True)         # 자기 점검용 정답은 남기지 않는다
    harness_check()
    rows, table = pilot_runs()
    if "A" in TRACKS:   # 트랙 A는 선별 후보 중 하나 이상이 guard를 통과해야 한다. 기준선은 세지 않는다
        base_ids = {b.get("id") for b in json.loads(read(OUT / "02_ideation" / "02_selected.json", "{}"))
                    .get("sota_baselines") or []}
        ok, why = any(v.get("guard_pass") for k, v in rows.items() if k not in base_ids), \
            "선별 후보 중 guard를 통과한 것이 없음 (기준선은 세지 않음)"
    else:
        ok, why = any(v.get("guard_pass") for v in rows.values()), "guard 통과 0건"
    if not ok:
        raise Stop(f"파일럿 신호 없음: {why}\n{table}")


def explain_scoring():
    # 채점 코드가 실제로 하는 일을 다른 모델이 읽고 자연어로 설명한다. 사람이 코드를 읽지 않고 관문에서 확인하도록.
    # explainer가 명세에 없으면 metrics.py 원문을 보여 준다. 채점 코드가 바뀌었을 때만 다시 부른다
    ex, out = STAGES["phase3_pilot"].get("explainer"), OUT / "03_pilot" / "scoring_explained.md"
    if not ex:
        return f"metrics.py\n```\n{read(EVAL / 'metrics.py')}\n```"
    codes = sorted(p for p in EVAL.iterdir() if p.suffix in (".py", ".sh"))
    if not out.exists() or any(p.stat().st_mtime > out.stat().st_mtime for p in codes):
        agent("explainer", ex, prompt("phase3_explain.md", tracks=",".join(TRACKS),
                                      contract=json.dumps(CONTRACT, ensure_ascii=False),
                                      pilot_check=(CFG.get("falsification") or {}).get("pilot_check", "-"),
                                      code="\n\n".join(f"### {p.name}\n```\n{read(p)}\n```" for p in codes)),
              out, "readonly", human=True)
    return (f"채점 방식 (설명: {ex['provider']}/{ex['model']}, 채점 코드를 읽고 쓴 것):\n{read(out).strip()}\n\n"
            f"코드 원문: {EVAL}/ ({', '.join(p.name for p in codes)})")


def phase_gate_pilot():
    while True:
        say(f"[{RUN_ID}] 파일럿 완료. 채점 방식 확인이 필요합니다.\n\n"
            f"{read(OUT / '03_pilot' / 'pilot_table.md')}\n"
            f"파일럿 판정 기준: {(CFG.get('falsification') or {}).get('pilot_check', '-')}\n\n{explain_scoring()}")
        a = ask("Enter 또는 y: 잠그고 본 실험 | q: 종료 | 그 밖의 글: 평가 코드 수정 지시 > ")
        if a.lower() in ("", "y"):
            lock_eval()
            git("add", "-A")                             # 파일럿 코드를 기준 커밋으로 (Phase 4의 reset·clean이 지우지 않게)
            git("commit", "--allow-empty", "-q", "-m", "pilot: 평가 하네스 승인 시점")
            STATE["best_commit"] = git("rev-parse", "HEAD")
            save_state()
            return
        if a.lower() == "q":
            raise Stop("사용자가 파일럿 확인 단계에서 종료", by_user=True)
        agent("builder", STAGES["phase3_pilot"]["builder"], prompt("phase3_fix.md", instruction=a, eval_dir=EVAL),
              OUT / "03_pilot" / f"fix_{time.strftime('%H%M%S')}.md", "full", env=BUILD_ENV)
        shutil.rmtree(SELFTEST, ignore_errors=True)
        harness_check()
        pilot_runs()


# ---- Phase 4 트랙 A/B: 시도 1회 = 에이전트 수정 → 제어기 실행·채점 → 채택(commit) 또는 원복(reset)
def phase_track_ab(track):
    st = STAGES[f"phase4_track_{track.lower()}"]
    role = "experimenter" if track == "A" else "worker"
    tsv, notes = MAIN / "results.tsv", MAIN / "notes.md"
    MAIN.mkdir(parents=True, exist_ok=True)
    if not tsv.exists():
        tsv.write_text("iter\tcommit\tstatus\tguard_pass\tprimary_metric\tspread\twall_time_sec\tdelta_pct\tsummary\n")
    T = STATE.setdefault(f"track_{track}", {"iter": 0, "stale": 0, "best_metric": None, "advice": "없음"})
    git("checkout", "-q", BRANCH)                       # 트랙 B 교대 측정 중에 끊겼으면 detached 상태일 수 있다
    git("reset", "--hard", STATE["best_commit"])        # 중단된 시도가 남아 있으면 정리 (--resume 대비)
    git("clean", "-fd")

    def row(it, commit, status, s, delta, summary):
        with open(tsv, "a") as f:
            f.write("\t".join(map(str, [f"{track}{it}", commit, status, s.get("guard_pass"), s.get("primary_metric"),
                                        s.get("spread"), s.get("wall_time_sec"), delta,
                                        re.sub(r"\s+", " ", summary)[:300]])) + "\n")

    if track == "B" and T["best_metric"] is None:       # 기준: 수정 전 코드
        s = run_case(MAIN / "B_iter_00_baseline", EVAL / "run_bench.sh")
        if not s.get("guard_pass"):
            raise RuntimeError("수정 전 코드가 guard를 통과하지 못함. 평가 하네스를 확인하세요")
        T["best_metric"], T["start_commit"] = s["primary_metric"], STATE["best_commit"]
        row(0, STATE["best_commit"][:9], "keep", s, None, "수정 전 기준 측정")
        save_state()

    # 트랙 B는 선별 후보를 유망한 순서(selector가 정한 순서)대로 시도마다 하나씩 배정한다
    cands = (json.loads(read(OUT / "02_ideation" / "02_selected.json", "{}")).get("selected_candidates") or []) \
        if track == "B" else []
    if len(cands) > BUDGET["max_iterations"] and not T.get("warned"):
        skipped = ", ".join(str(c.get("id", "?")) for c in cands[BUDGET["max_iterations"]:])
        say(f"[{RUN_ID}] 경고: 선별 후보 {len(cands)}개에 시도 상한은 {BUDGET['max_iterations']}회입니다.\n"
            f"유망도가 낮은 후보 {skipped}는 시도하지 못합니다.")
        T["warned"] = True
        save_state()

    while T["iter"] < BUDGET["max_iterations"]:
        T["iter"] += 1
        it = T["iter"]
        save_state()
        work = MAIN / f"{track}_iter_{it:02d}"
        cur = slot(f"{track}_{role}", st[role])
        cand = cands[it - 1] if it <= len(cands) else None
        assigned = json.dumps(cand, ensure_ascii=False, indent=1) if cand else \
            "없음. 선별 후보는 모두 시도했다. 결과와 노트를 보고 다음 방법을 직접 고른다." 
        text = llm.call(cur, prompt(
            f"track_{track.lower()}.md", iter=it, max_iter=BUDGET["max_iterations"], spec=SPEC_TEXT,
            selected=read(OUT / "02_ideation" / "02_selected.json"), pilot=read(OUT / "03_pilot" / "pilot_table.md"),
            results=tail(tsv), notes=tail(notes, 60), advice=T["advice"], iter_dir=work, eval_dir=EVAL,
            best_commit=STATE["best_commit"], best_metric=T["best_metric"], direction=DIRECTION,
            min_pct=CONTRACT.get("min_improvement_pct", 1.0), build_cmd=CFG.get("build_cmd") or "없음",
            seeds=BUDGET.get("seeds"), rounds=BUDGET.get("bench_rounds"), assigned=assigned,
            comparison_all=read(MAIN / "comparison_all.md", "아직 없음 (첫 시도)"),
            regimes=", ".join(sorted(p.name for p in (EVAL / "inputs").glob("*") if p.is_dir())) or "main"),
            work / "agent.md", mode="full", cwd=REPO, env=ENV)
        if track == "A":
            s = attempt_a(work)
            delta = None
            keep = bool(s.get("guard_pass"))
        else:
            s = attempt_b(work)
            delta = s.get("delta_pct")
            keep = bool(s.get("guard_pass")) and delta is not None and delta >= CONTRACT.get("min_improvement_pct", 1.0)
        disc = [ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.startswith("DISCOVERY:")][:3]
        summary = (f"[{cand.get('id')}] " if cand else "") + ("; ".join(disc) or s.get("guard_reason", ""))
        if keep:
            if track == "A":
                git("add", "-A")
                git("commit", "--allow-empty", "-q", "-m", f"A iter {it}: {summary[:60]}")
            else:                                       # 후보는 attempt_b에서 이미 커밋됨. 메시지만 바꾼다
                git("commit", "--amend", "--allow-empty", "-q", "-m", f"B iter {it}: {summary[:60]}")
            STATE["best_commit"] = git("rev-parse", "HEAD")
            if track == "A":                            # 채택된 시도의 경우를 누적표에 합친다 (같은 경우는 최신 값으로)
                T.setdefault("cases", {}).update({k: {**v, "attempt": f"A{it}", "commit": STATE["best_commit"]}
                                                 for k, v in s["cases"].items()})
                (MAIN / "comparison_all.md").write_text(case_table(T["cases"]))
            if track == "B" and (T["best_metric"] is None or (delta or 0) > 0):   # 트랙 A는 경우별 표(comparison.md)로 본다
                T["best_metric"] = s["primary_metric"]
            T["stale"] = 0
        else:
            git("reset", "--hard", STATE["best_commit"])
            git("clean", "-fd")
            T["stale"] += 1
        status = "keep" if keep else ("crash" if s["status"] == "crash" else "discard")
        if track == "A" and s.get("cases"):             # 트랙 A는 경우(모델 × regime)마다 한 줄
            for i, (name, c) in enumerate(s["cases"].items()):
                row(f"{it}:{name}", STATE["best_commit"][:9] if keep else "-", status, c, None, summary if i == 0 else "")
        else:
            row(it, STATE["best_commit"][:9] if keep else "-", status, s, delta, summary)
        with open(notes, "a") as f:
            f.write(f"- {track} iter {it} [{status}] {summary}\n")
        if T["stale"] >= 2:                              # 두 번 연속 실패나 정체: 한 칸 승격 + (B) 다른 계열 자문
            STATE["slots"][f"{track}_{role}"] = llm.escalate(cur)
            if track == "B" and st.get("unstuck_peer"):
                T["advice"] = agent("B_unstuck", st["unstuck_peer"], prompt(
                    "track_b_unstuck.md", spec=SPEC_TEXT, results=tail(tsv), notes=tail(notes, 60),
                    best_commit=STATE["best_commit"]), work / "unstuck.md", "readonly")
            T["stale"] = 0
        save_state()
        if re.search(r"^NEXT:\s*done", text, re.M | re.I) and it >= len(cands):   # 후보가 남아 있으면 done을 무시
            break


# ---- Phase 4 끝: 결론 수치를 위한 마지막 측정 (모델 호출 없음)
def phase_final_test_a():
    # 본 실험은 검증 입력의 점수를 보며 시도를 고르므로 그 점수는 실제보다 좋게 나온다. 끝난 뒤 한 번만,
    # 누적표의 경우마다 그 값을 낸 시도의 코드(커밋과 run.sh)를 처음 보는 최종 시험 입력과 새 시드로 다시 채점한다.
    # 최종 입력은 검증 입력 자리(inputs/)에 잠시 바꿔 끼운다. 실험 코드는 그대로 두고, 수리도 하지 않는다
    val, fin, park = EVAL / "inputs", HIDDEN / "final" / "inputs", EVAL / "inputs_val"
    if park.exists():                                   # 바꿔 끼운 채로 끊긴 실행이면 먼저 되돌린다
        if val.exists() and not fin.exists():
            val.rename(fin)
        park.rename(val)
    cases = STATE["track_A"].get("cases") or {}
    if not cases:
        (MAIN / "final_test.md").write_text("채택된 시도가 없어 최종 시험을 하지 않았다.\n")
        return
    verify_locks()
    groups = {}
    for name, c in cases.items():
        groups.setdefault((c["attempt"], c.get("commit") or STATE["best_commit"]), []).append(name)
    per, base, seeds = {}, MAIN / "final_test", [1000 + sd for sd in range(BUDGET["seeds"])]
    shutil.rmtree(base, ignore_errors=True)
    val.rename(park)
    fin.rename(val)
    try:
        for (att, commit), names in sorted(groups.items()):
            git("checkout", "-q", "-f", "--detach", commit)
            git("clean", "-fd")
            for sd in seeds:
                res = base / att / f"seed_{sd}" / "result"
                rc, log, wall = run_once(MAIN / f"A_iter_{int(att[1:]):02d}" / "run.sh", res, sd)
                (res.parent / "run.log").write_text(log)
                for n in names:
                    per.setdefault(n, []).append(
                        score(res / n, wall, n.split("@", 1)[1] if "@" in n else None, final=True)
                        if rc == 0 and (res / n).is_dir() else
                        {"guard_pass": False, "primary_metric": None,
                         "guard_reason": f"비정상 종료: {log[-200:]}" if rc else "경우 폴더 없음"})
    finally:
        val.rename(fin)
        park.rename(val)
        git("checkout", "-q", "-f", BRANCH)
        git("clean", "-fd")
    verify_locks()
    fc = summarize(per)
    (MAIN / "final_test.json").write_text(json.dumps(fc, ensure_ascii=False, indent=2))
    m = CONTRACT.get("metric", "metric")
    (MAIN / "final_test.md").write_text(
        f"최종 시험 (본 실험에서 채점하지 않은 입력, 시드 {seeds[0]}~{seeds[-1]})\n\n"
        f"| 경우 | 검증 {m} 평균 | 최종 시험 {m} 평균 | 최종 표준오차 | 시드 수 | guard | 시도 |\n|---|---|---|---|---|---|---|\n"
        + "".join(f"| {k} | {cases[k]['primary_metric']} | {v['primary_metric']} | {v['spread']} | {v['n_seeds']} | "
                  f"{v['guard_pass']} | {cases[k]['attempt']} |\n" for k, v in sorted(fc.items())))


def phase_confirm_b():
    # 시도마다 '그때의 최고' 대비로 채택하므로, 측정 잡음으로 우연히 좋게 나온 채택이 쌓이면 개선 폭이 부풀려진다.
    # 끝난 뒤 한 번만, 수정 전 코드와 최종 코드를 번갈아 재서(라운드는 시도 때의 두 배) 중앙값을 비교한다
    T = STATE["track_B"]
    start, best = T["start_commit"], STATE["best_commit"]
    if start == best:
        (MAIN / "B_confirm.md").write_text("채택된 시도가 없어 최종 비교를 하지 않았다.\n")
        return
    work, rounds = MAIN / "B_confirm", 2 * BUDGET["bench_rounds"]
    shutil.rmtree(work, ignore_errors=True)
    runs = bench_pair(work, {"original": start, "final": best}, rounds)
    med = {k: statistics.median(nums(v)) if nums(v) else None for k, v in runs.items()}
    ok = {k: all(x.get("guard_pass") for x in v) for k, v in runs.items()}
    out = {"original_commit": start, "final_commit": best, "rounds": rounds, "original_median": med["original"],
           "final_median": med["final"], "delta_pct": pct(med["final"], med["original"]), "final_guard_pass": ok["final"],
           "values": {k: nums(v) for k, v in runs.items()}}
    (MAIN / "B_confirm.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    m = CONTRACT.get("metric", "metric")
    (MAIN / "B_confirm.md").write_text(
        f"최종 비교 (수정 전 코드와 최종 코드를 번갈아 {rounds}번씩 잰 중앙값)\n\n"
        f"| 코드 | 커밋 | {m} 중앙값 | 측정값 | guard |\n|---|---|---|---|---|\n"
        + "".join(f"| {name} | {c[:9]} | {med[k]} | {nums(runs[k])} | {ok[k]} |\n"
                  for name, k, c in (("수정 전", "original", start), ("최종", "final", best)))
        + f"\n개선 폭 (좋아진 쪽이 양수): {out['delta_pct']}%\n")


# ---- Phase 4 트랙 C: 증명 ↔ 반박·반례 탐색을 라운드로 반복
HUNT_CATS = ("extreme", "singular", "equality", "typical")   # 양극단, 특이점, 등호 근처, 무던한 값


def run_hunter(rd):
    script = rd / "counterexample_hunter.py"
    for attempt in range(BUDGET["max_crash_repairs"] + 1):
        r = sh([PY, str(script)], cwd=rd, check=False) if script.exists() else None
        log = f"{r.stdout}{r.stderr}" if r else "counterexample_hunter.py 없음"
        (rd / "hunter.log").write_text(log)
        j = last_json(r.stdout) if r and r.returncode == 0 else None
        if j:
            # 네 범주를 모두 시험해야 한다. 등호 근처는 등호가 성립하는 입력이 없다는 이유를 적으면 0이어도 된다
            cats = j.get("by_category") if isinstance(j.get("by_category"), dict) else {}
            miss = [c for c in HUNT_CATS if not (isinstance(cats.get(c), int) and cats[c] > 0)
                    and not (c == "equality" and j.get("no_equality_reason"))]
            return {**{k: j.get(k) for k in ("tested_cases", "counterexamples_found", "worst_counterexample",
                                              "tightness_ok", "no_equality_reason")},
                    "by_category": cats, "coverage_missing": miss}
        if attempt < BUDGET["max_crash_repairs"]:
            repair(rd, f"cd {rd} && python counterexample_hunter.py", log, attempt + 1)
    return {"tested_cases": 0, "counterexamples_found": None, "worst_counterexample": None, "tightness_ok": False,
            "coverage_missing": list(HUNT_CATS)}


def phase_track_c():
    st = STAGES["phase4_track_c"]
    C = STATE.setdefault("track_C", {"round": 0, "verdict": None})
    while C["round"] < BUDGET["max_debate_rounds"]:
        C["round"] += 1
        n = C["round"]
        save_state()
        rd, prev = MAIN / f"C_round_{n:02d}", MAIN / f"C_round_{n - 1:02d}"
        proof = agent("C_prover", st["prover"], prompt(
            "track_c_prove.md", n=n, spec=SPEC_TEXT, selected=read(OUT / "02_ideation" / "02_selected.json"),
            known=read(OUT / "01_recon.md"),
            prev_proof=read(prev / "proof.md", "없음 (첫 라운드)"), objections=read(prev / "refute.md", "없음"),
            verdict=json.dumps(C["verdict"], ensure_ascii=False) if C["verdict"] else "없음"),
            rd / "proof.md", "readonly")
        try:
            _, logic = agent_json("C_refuter", st["refuter"], prompt(
                "track_c_refute.md", n=n, spec=SPEC_TEXT, proof=proof, round_dir=rd, known=read(OUT / "01_recon.md"),
                prev_missing=", ".join((C["verdict"] or {}).get("coverage_missing") or []) or "없음"),
                rd / "refute.md", "full", parse_verdict, VERDICT_SCHEMA)
        except ValueError:
            logic = {"logic_sound": False, "fatal_flaws": ["반박 모델의 판정 JSON을 다시 요청해도 받지 못함"]}
        v = {**run_hunter(rd), **{k: logic.get(k) for k in ("logic_sound", "scope_realistic", "non_trivial", "fatal_flaws")}}
        (MAIN / f"round_{n}_verdict.json").write_text(json.dumps(v, ensure_ascii=False, indent=2))
        C["verdict"] = v
        save_state()
        if v["logic_sound"] and v["counterexamples_found"] == 0 and (v["tested_cases"] or 0) > 0 \
                and v["scope_realistic"] and v["non_trivial"] and v["tightness_ok"] and not v["coverage_missing"]:
            return
    v = C["verdict"]
    if not v["logic_sound"] or v["counterexamples_found"] != 0 or not v["tested_cases"]:
        raise Stop(f"트랙 C 기각: 논리 결함 또는 수치 반례\n{json.dumps(v, ensure_ascii=False)}")
    last = MAIN / f"C_round_{C['round']:02d}"
    say(f"[{RUN_ID}] 트랙 C: 논리와 수치 반례 검사는 통과했지만 일반성·비자명성·밀착도·반례 탐색 범위 중 통과하지 못한 것이 있음.\n"
        f"판정: {json.dumps(v, ensure_ascii=False)}\n\n증명 앞부분:\n{read(last / 'proof.md')[:1500]}\n\n"
        f"반박 요지:\n{read(last / 'refute.md')[-1500:]}")
    if ask("Enter: Phase 5 감사 진행 | q: 폐기하고 종료 > ").lower() == "q":
        raise Stop("트랙 C 결과를 사용자가 폐기", by_user=True)


# ---- Phase 5: 다른 계열 모델의 감사 → 보고서
def phase_audit():
    st = STAGES["phase5_audit"]
    files = sorted(str(p.relative_to(REPO)) for p in OUT.rglob("*")
                   if p.is_file() and p.suffix in (".json", ".tsv", ".csv", ".log", ".md", ".py", ".sh"))
    verdicts = "\n".join(f"{p.name}: {p.read_text()}" for p in sorted(MAIN.glob("round_*_verdict.json"))) or "없음"
    final = "\n\n".join(read(MAIN / f) for f in ("final_test.md", "B_confirm.md") if (MAIN / f).exists()) or "없음"
    ctx = dict(spec=SPEC_TEXT, tracks=",".join(TRACKS), files="\n".join(files[:400]), results=tail(MAIN / "results.tsv", 80),
               verdicts=verdicts, selected=read(OUT / "02_ideation" / "02_selected.json"), out_dir=OUT, final=final)
    texts = parallel([(s, prompt("phase5_audit.md", **ctx), OUT / "05_audit" / f"auditor_{i:02d}.md", "readonly", True)
                      for i, s in enumerate(st["auditors"], 1)])
    (OUT / "05_audit.md").write_text("\n\n".join(
        f"# auditor_{i:02d} ({s['provider']}/{s['model']}/{s['effort']})\n\n{t}"
        for i, (s, t) in enumerate(zip(st["auditors"], texts), 1)))
    items, unformatted = [], []                         # 치명 지적. 완료 알림에 reporter의 판단과 함께 띄운다
    for i, (s, t) in enumerate(zip(st["auditors"], texts), 1):
        who, j = f"auditor_{i:02d} ({s['provider']}/{s['model']})", last_json(t)
        if isinstance(j, dict) and isinstance(j.get("fatal"), list):
            items += [{"who": who, "text": str(x)} for x in j["fatal"]]
        else:
            unformatted.append(who)
    verify_locks()
    text = agent("reporter", st["reporter"], prompt(
        "phase5_report.md", **ctx, critique=read(OUT / "05_audit.md"), notes=tail(MAIN / "notes.md", 80),
        env=read(OUT / "env.txt")[:3000],
        fatal="".join(f"{n}. {x['who']}: {x['text']}\n" for n, x in enumerate(items, 1)) or "없음"),
        OUT / "06_report.md", "readonly", human=True)
    blocks = JUDGE_RE.findall(text)                     # 판단 블록은 알림용이라 보고서에서 뗀다
    (OUT / "06_report.md").write_text(JUDGE_RE.sub("\n\n", text).strip() + "\n")
    if items:
        try:
            got = judgments(json.loads(blocks[-1]))
        except (IndexError, KeyError, TypeError, ValueError):
            got = {}                                    # 알림에 '판단 없음, 보고서 4절 확인'으로 나간다
        for n, x in enumerate(items, 1):
            x["verdict"], x["note"] = got.get(n, (None, ""))
    (OUT / "05_audit" / "fatal.json").write_text(
        json.dumps({"items": items, "unformatted": unformatted}, ensure_ascii=False, indent=2))
    if st.get("checker"):
        check_report(st["checker"], ctx)


def check_report(ch, ctx):
    # 보고서를 다른 벤더 모델이 결과 파일과 대조한다. 검토자는 고칠 문장(원문 그대로)과 고친 문장만 내고,
    # 제어기가 그 문장만 바꾼다(나머지 글은 그대로). 고친 내용은 보고서 끝 '검토 기록'에 남긴다
    draft = read(OUT / "06_report.md")
    (OUT / "06_report_draft.md").write_text(draft)
    try:
        _, fixes = agent_json("checker", ch, prompt("phase5_check.md", **ctx, report=draft,
                                                    comparison=read(MAIN / "comparison_all.md", "없음")),
                              OUT / "05_audit" / "report_check.md", "readonly", parse_fixes, FIX_SCHEMA)
    except ValueError:
        fixes = None
    rep, done, missed = draft, [], []
    for f in fixes or []:
        if f["find"] and f["find"] in rep:
            rep = rep.replace(f["find"], f["replace"], 1)
            done.append(f)
        else:
            missed.append(f)
    who = f"{ch['provider']}/{ch['model']}"
    if fixes is None:
        log = f"검토: {who}. 검토 응답의 형식을 받지 못해 고치지 않았다."
    else:
        log = f"검토: {who}. 수치와 결론을 결과 파일과 대조해 고친 곳 {len(done)}개.\n" + "".join(
            f"- \"{f['find']}\" → \"{f['replace']}\" ({f.get('why', '')})\n" for f in done) + "".join(
            f"- 보고서에서 원문을 찾지 못해 고치지 않은 지적: \"{f['find']}\" ({f.get('why', '')})\n" for f in missed)
    (OUT / "06_report.md").write_text(f"{rep.rstrip()}\n\n## 검토 기록\n\n{log}")


# ---- 마지막: 보고서 확인 → 커밋 하나로 합쳐 원본 저장소의 새 브랜치로 가져와 push
def scrub():
    # 산출물 텍스트의 절대경로를 자리표시자로 바꾼다 (긴 경로부터)
    subs = sorted({str(REPO): "<repo>", str(VENV): "<env>", str(RUN): "<run>", str(SRC): "<repo>",
                   str(Path.home()): "~"}.items(), key=lambda kv: -len(kv[0]))
    for p in OUT.rglob("*"):
        if p.is_file() and p.suffix in (".md", ".json", ".tsv", ".csv", ".log", ".txt", ".py", ".sh", ".yaml"):
            t = p.read_text(errors="replace")
            new = t
            for a, b in subs:
                new = new.replace(a, b)
            if new != t:
                p.chmod(0o644)
                p.write_text(new)


def push_gate(ref):
    # ref에서 바뀐 파일의 절대경로, 호스트명, 토큰 꼴 문자열, 큰 파일을 찾는다. 찾은 줄의 내용은 보여 주지 않는다(토큰 노출 방지).
    # 작업 트리는 이때 (최고 커밋 + 산출물) 상태라 ref의 내용과 같다.
    pats = [re.escape(str(Path.home())), r"ghp_[A-Za-z0-9]{20,}", r"github_pat_\w{20,}", r"sk-[A-Za-z0-9_-]{20,}",
            r"xox[abprs]-[A-Za-z0-9-]{10,}", r"hooks\.slack\.com/services/", r"pypi-[A-Za-z0-9_-]{20,}", r"AKIA[0-9A-Z]{16}"]
    host = socket.gethostname()
    if len(host) >= 4:
        pats.append(re.escape(host))
    pat, problems = re.compile("|".join(pats)), []
    for f in git("diff", "--name-only", STATE["base_commit"], ref).splitlines():
        p = REPO / f
        if not p.is_file():
            continue
        if p.stat().st_size > PROV["push"]["max_file_mb"] * 1e6:
            problems.append(f"{f}: {PROV['push']['max_file_mb']} MB 초과")
            continue
        try:
            lines = p.read_text().splitlines()
        except UnicodeDecodeError:
            continue
        problems += [f"{f}:{i}" for i, ln in enumerate(lines, 1) if pat.search(ln)]
    return problems


def remote_is_private(remote):
    # GitHub 원격만 확인한다: 인증 없이 조회되면 공개 저장소다.
    r = sh(["git", "remote", "get-url", remote], cwd=SRC, check=False)
    if r.returncode:
        return False, f"원본에 원격 '{remote}'가 없음"
    url = r.stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        return False, f"GitHub 원격이 아니라 공개 여부를 확인하지 못함 ({url})"
    try:
        urllib.request.urlopen(f"https://api.github.com/repos/{m[1]}/{m[2]}", timeout=10)
    except urllib.error.HTTPError as e:
        return (True, "") if e.code == 404 else (False, f"GitHub 조회 오류 {e.code}")
    except OSError as e:
        return False, f"GitHub 조회 실패: {e}"
    return False, "공개 저장소"


def phase_final():
    rep = read(OUT / "06_report.md")
    heads = [m.start() for m in re.finditer(r"^## ", rep, re.M)]   # 알림에는 결론 절(첫 절)까지만 보낸다
    # 알림의 결론만 읽고 넘어가지 않게, 감사의 치명 지적과 그에 대한 reporter의 판단을 결론 앞에 붙인다
    fatal = json.loads(read(OUT / "05_audit" / "fatal.json", "{}"))
    items = fatal.get("items") or []
    warn = (f"감사 치명 지적 {len(items)}개\n" + "".join(
        f"- {x['who']}: {x['text']}\n  → reporter: "
        + (f"{x['verdict']}. {x['note']}\n" if x.get("verdict") else "판단 없음, 보고서 4절 확인\n") for x in items)
        + "\n" if items else "") \
        + "".join(f"{who}: 치명 지적 목록을 형식대로 받지 못함. 05_audit.md 확인\n\n" for who in fatal.get("unformatted") or [])
    warn += "".join(f"참고: {a}\n" for a in ADAPT) + ("\n" if ADAPT else "")
    say(f"[{RUN_ID}] 완료\n{warn}보고서: {OUT / '06_report.md'}\n\n{(rep[:heads[1]] if len(heads) > 1 else rep[:3000]).strip()}")
    if ask(f"보고서를 확인했으면 원본({SRC})의 새 브랜치 {BRANCH}로 push할까요? [y/N] > ").lower() != "y":
        raise Stop("push 보류. 나중에 --resume으로 다시 묻습니다", by_user=True)
    final = f"{BRANCH}-final"
    git("checkout", "-q", BRANCH)
    git("reset", "--hard", STATE["best_commit"])
    git("clean", "-fd")
    STATE["status"] = "PUSHING"
    save_state()
    shutil.copy2(STATE_PATH, OUT / "state.json")
    scrub()
    # 시도별 커밋 이력은 클론에만 남기고, 원본에는 (최고 커밋 트리 + 산출물)을 커밋 하나로 보낸다.
    # 브랜치를 checkout하지 않고 임시 index로 만든다: checkout하면 산출물 폴더가 작업 트리에서 지워질 수 있다.
    genv = dict(ENV, GIT_INDEX_FILE=str(RUN / "final.index"))
    sh(["git", "read-tree", STATE["best_commit"]], env=genv)
    sh(["git", "add", "-f", f"research_runs/{RUN_ID}"], env=genv)
    tree = sh(["git", "write-tree"], env=genv).stdout.strip()
    commit = git("commit-tree", tree, "-p", STATE["base_commit"], "-m", f"autoresearch: {RUN_ID}")
    git("branch", "-f", final, commit)
    (RUN / "final.index").unlink()
    problems = push_gate(final)
    if problems:
        raise Stop("push 관문에서 걸림 (절대경로·호스트명·토큰 꼴·큰 파일). 클론에서 고친 뒤 --resume\n" + "\n".join(problems[:50]),
                   by_user=True)   # push 확인 직후라 사람이 화면 앞에 있다. Slack으로 다시 보내지 않는다
    remote = CFG.get("push_remote", "origin")
    ok, why = remote_is_private(remote)
    git("fetch", "-q", str(REPO), f"+{final}:{BRANCH}", cwd=SRC)   # 다시 시도하면 이 도구가 만든 브랜치를 덮어쓴다
    if not ok:
        raise Stop(f"원본에 브랜치 {BRANCH}는 만들었지만 push하지 않음: {why}\n직접 하려면: git -C {SRC} push {remote} {BRANCH}",
                   by_user=True)
    git("push", "-q", remote, BRANCH, cwd=SRC)
    print(f"[{RUN_ID}] push 완료: {remote} {BRANCH}", flush=True)


# ---- 실행
PHASES = [("workspace", phase_workspace), ("recon", phase_recon), ("ideation", phase_ideation)]
if {"A", "B"} & set(TRACKS):
    PHASES += [("pilot", phase_pilot), ("gate_pilot", phase_gate_pilot)]
for t in TRACKS:
    PHASES.append((f"track_{t}", phase_track_c if t == "C" else (lambda t=t: phase_track_ab(t))))
    if t == "A":
        PHASES.append(("final_test_A", phase_final_test_a))
    if t == "B":
        PHASES.append(("confirm_B", phase_confirm_b))
PHASES += [("audit", phase_audit), ("final", phase_final)]

save_state()
try:
    for name, fn in PHASES:
        if name in STATE["done"]:
            continue
        STATE["phase"] = name
        save_state()
        print(f"\n== {name}", flush=True)
        fn()
        STATE["done"].append(name)
        save_state()
    STATE["status"] = "DONE"
except Stop as e:
    STATE["status"], STATE["stop_reason"] = "STOPPED", str(e)
    (print if e.by_user else say)(f"[{RUN_ID}] 멈춤 (단계 {STATE['phase']}): {e}")
except Exception:
    STATE["status"] = "FAILED"
    say(f"[{RUN_ID}] 오류로 멈춤 (단계 {STATE['phase']}). 고친 뒤 --resume\n```\n{traceback.format_exc()[-1500:]}\n```")
    raise
finally:
    save_state()
