# 자율 연구 제어기. tmux 안에서 실행한다.
#   python ~/autoresearch/run_research.py --spec ~/autoresearch_runs/<프로젝트>/<run_id>/00_spec.md [--resume]
# 순서: 작업공간 → 1 조사 → 1.5 발상·선별 → 2 파일럿·채점기 승인(트랙 A/B) → 3 트랙 A/B/C → 4 감사·보고 → push 승인
import argparse
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
import urllib.request
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
SELFTEST = OUT / "02_pilot" / "_selftest_hidden"   # builder가 자기 점검에 쓰는 임시 정답 폴더. 호출이 끝나면 지운다
MAIN = OUT / "03_main"
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
NEED = ["phase1_recon", "phase1_5_ideation", "crash_repair", "phase4_audit"] \
    + (["phase2_pilot"] if {"A", "B"} & set(TRACKS) else []) + [f"phase3_track_{t.lower()}" for t in TRACKS]
NEED_BUDGET = ["max_iterations", "max_crash_repairs", "max_debate_rounds", "run_limits"] \
    + (["seeds"] if "A" in TRACKS else []) + (["bench_rounds"] if "B" in TRACKS else [])
MISSING = [f"stages.{k}" for k in NEED if k not in STAGES] + [f"budget.{k}" for k in NEED_BUDGET if k not in BUDGET]
if MISSING:
    raise SystemExit(f"명세에 빠진 항목: {', '.join(MISSING)} (templates/00_spec_template.md 참고)")
LIMITS = BUDGET["run_limits"]
CONTRACT = CFG.get("evaluation_contract") or {}
DIRECTION = CONTRACT.get("direction", "higher_is_better")

# 작성 모델과 반박·감사 모델은 계열(벤더)이 달라야 한다. 한도 초과로 대체된 뒤까지는 보장하지 않는다.
MAIN_PROVIDERS = {STAGES[f"phase3_track_{t.lower()}"][r]["provider"]
                  for t, r in (("A", "experimenter"), ("B", "worker"), ("C", "prover")) if t in TRACKS}
if "C" in TRACKS and STAGES["phase3_track_c"]["prover"]["provider"] == STAGES["phase3_track_c"]["refuter"]["provider"]:
    raise SystemExit("트랙 C의 prover와 refuter는 다른 벤더여야 합니다")
if MAIN_PROVIDERS & {a["provider"] for a in STAGES["phase4_audit"]["auditors"]}:
    raise SystemExit(f"Phase 4 감사자에 본 실험 벤더({', '.join(MAIN_PROVIDERS)})가 들어 있습니다")
SPLIT_LOCK = (CFG.get("constraints") or {}).get("split_lock") or []
SPLIT_LOCK = [SPLIT_LOCK] if isinstance(SPLIT_LOCK, str) else SPLIT_LOCK
SECOND_PASS = (CFG.get("constraints") or {}).get("timing_rule") == "second_pass"

# ---- 상태
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


def visible_locks():
    # 에이전트에게 보여 줄 잠긴 파일 (클론 기준 경로). 숨긴 정답 파일은 이름도 알려 주지 않는다
    return [str(Path(k).relative_to("repo")) for k in STATE["locks"] if k.startswith("repo/")]


TIMEOUT = BUDGET.get("call_timeout_min")
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


def agent(key, default, body, out_file, mode, human=False, env=None):
    return llm.call(slot(key, default), body, out_file, mode=mode, cwd=REPO, env=env or ENV, human=human)


SELECT_SCHEMA = ('```json\n{"sota_baselines": [{"id": "BASE_1", "name": "...", "kind": "classical|quantum", "source": "..."}],\n'
                 ' "selected_candidates": [{"id": "D1_A", "title": "...", "mechanism": "...", "quantum_rationale": "(트랙 A만)", '
                 '"pilot_setup": "...", "rank_reason": "..."}],\n "dropped_candidates": [{"id": "D2_A", "reason": "..."}]}\n```')
VERDICT_SCHEMA = '출력 마지막 줄에 JSON 한 줄: {"logic_sound": true, "scope_realistic": true, "non_trivial": true, "fatal_flaws": []}'


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


def agent_json(key, default, body, out_file, mode, parse, schema):
    # 정해진 JSON 형식이 틀리면 같은 모델에게 형식만 고쳐 다시 내라고 한 번 요청한다. 두 번째도 틀리면 ValueError
    text = agent(key, default, body, out_file, mode)
    try:
        return text, parse(text)
    except ValueError:
        fixed = agent(key, default, prompt("json_fix.md", schema=schema, text=text[-8000:]),
                      Path(out_file).with_name(Path(out_file).stem + "_jsonfix.md"), "readonly")
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


def score(res, wall, regime=None):
    # metrics.py는 제어기만 돌리고, 이때만 숨긴 정답 경로를 넘긴다. regime은 prepare.py가 미리 만든 입력 변형의 이름
    # ponytail: 에이전트와 같은 리눅스 사용자라 정답 폴더를 일부러 찾아 읽는 것까지는 막지 못한다 (막으려면 별도 사용자나 컨테이너)
    verify_locks()
    r = sh([PY, str(EVAL / "metrics.py"), str(res)] + ([regime] if regime else []), check=False, env=SCORE_ENV)
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
    cases = {}
    for name, runs in per.items():
        vals = nums(runs)
        cases[name] = {"guard_pass": all(r.get("guard_pass") for r in runs) and len(vals) == BUDGET["seeds"],
                       "primary_metric": statistics.mean(vals) if vals else None,
                       "spread": statistics.stdev(vals) / len(vals) ** 0.5 if len(vals) > 1 else None,
                       "n_seeds": len(vals), "wall_time_sec": statistics.mean(nums(runs, "wall_time_sec") or [0]),
                       "guard_reason": next((r.get("guard_reason", "") for r in runs if not r.get("guard_pass")), "")}
    (work / "comparison.md").write_text(case_table(cases))   # 제어기가 채점한 이번 시도의 경우별 비교표
    out = {"status": "ok", "cases": cases, "guard_pass": all(v["guard_pass"] for v in cases.values()),
           "guard_reason": next((f"{k}: {v['guard_reason']}" for k, v in cases.items() if not v["guard_pass"]), "")}
    (work / "score.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    return out


def attempt_b(work):
    # 트랙 B 시도 1회: (1) 한 번 실행해 오류와 guard 확인 (crash면 수리) (2) 후보를 작업 브랜치에 임시 커밋
    # (3) 후보와 현재 최고 커밋을 번갈아 bench_rounds번씩 재고 중앙값을 비교한다 (라운드마다 순서를 바꿔 순서 효과를 줄인다)
    s = run_case(work / "check", EVAL / "run_bench.sh")
    if s["status"] == "crash" or not s.get("guard_pass"):
        return s
    git("add", "-A")
    git("commit", "--allow-empty", "-q", "-m", "candidate (측정 중)")
    cand, best = git("rev-parse", "HEAD"), STATE["best_commit"]
    runs = {"cand": [], "best": []}
    for r in range(BUDGET["bench_rounds"]):
        order = [("cand", cand), ("best", best)] if r % 2 == 0 else [("best", best), ("cand", cand)]
        for label, commit in order:
            git("checkout", "-q", "--detach", commit)
            d = work / f"round_{r + 1}_{label}"
            rc, log, wall = run_once(EVAL / "run_bench.sh", d / "result")
            sc = score(d / "result", wall) if rc == 0 else {
                "guard_pass": False, "primary_metric": None, "guard_reason": f"측정 중 비정상 종료: {log[-200:]}"}
            (d / "score.json").write_text(json.dumps(sc, ensure_ascii=False, indent=2))
            runs[label].append(sc)
    git("checkout", "-q", BRANCH)                       # 작업 브랜치는 후보 커밋을 가리킨다
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
    (OUT / "env.txt").write_text(f"{sh([PY, '--version']).stdout}base_commit {STATE['base_commit']}\n\n{v}")


# ---- Phase 1: 저장소 내부 자산 + 선행연구(SoTA 기준선, 이미 알려진 접근)
def phase_recon():
    slots = STAGES["phase1_recon"]["slots"]
    texts = parallel([(s, prompt(f"phase1_{s['role']}.md", spec=SPEC_TEXT), OUT / "01_recon" / f"recon_{i:02d}.md",
                       "readonly", True) for i, s in enumerate(slots, 1)])
    (OUT / "01_recon.md").write_text("\n\n".join(
        f"# {s['role']} ({s['provider']}/{s['model']}/{s['effort']})\n\n{t}" for s, t in zip(slots, texts)))


# ---- Phase 1.5: 발산 → 비평 → 선별. 통과 후보가 없으면 탈락 사유를 넣어 한 번만 다시 발산
def phase_ideation():
    st, recon, d = STAGES["phase1_5_ideation"], read(OUT / "01_recon.md"), OUT / "01b_ideation"
    feedback = "없음"
    for attempt in (1, 2):
        sfx = "" if attempt == 1 else "_retry"
        dtexts = parallel([(s, prompt("phase1_5_diverge.md", n=i, track=",".join(TRACKS), lens=s.get("lens", "자유"),
                                      spec=SPEC_TEXT, recon=recon, feedback=feedback),
                            d / f"diverger_{i:02d}{sfx}.md", "readonly", False)
                           for i, s in enumerate(st["divergers"], 1)])
        ideas = [f"## diverger_{i:02d}\n{t}" for i, t in enumerate(dtexts, 1)]
        crit = st.get("critics") or []
        shard = st.get("critics_mode", "all") == "shard"
        ctexts = parallel([(s, prompt("phase1_5_critic.md", spec=SPEC_TEXT, recon=recon,
                                      ideas="\n\n".join(ideas[i::len(crit)] if shard else ideas)),
                            d / f"critic_{i + 1:02d}{sfx}.md", "readonly", False)
                           for i, s in enumerate(crit)]) if crit else []
        _, sel = agent_json("selector", st["selector"], prompt(
            "phase1_5_select.md", spec=SPEC_TEXT, recon=recon, ideas="\n\n".join(ideas),
            critiques="\n\n".join(f"## critic_{i:02d}\n{t}" for i, t in enumerate(ctexts, 1)) or "비평 없음"),
            d / f"selector{sfx}.md", "readonly", parse_selection, SELECT_SCHEMA)
        (d / "01b_selected.json").write_text(json.dumps(sel, ensure_ascii=False, indent=2))
        if sel.get("selected_candidates"):
            return
        feedback = json.dumps(sel.get("dropped_candidates", []), ensure_ascii=False)
    raise Stop("통과 후보 0개 (다시 발산한 뒤에도)")


# ---- Phase 2: 평가 하네스 작성 → 자동 점검 → 파일럿 (트랙 A/B)
def harness_check():
    for attempt in range(BUDGET["max_crash_repairs"] + 1):
        shutil.rmtree(HIDDEN, ignore_errors=True)       # 정답은 매번 prepare.py로 새로 만든다
        HIDDEN.mkdir(parents=True)
        r1 = sh([PY, str(EVAL / "prepare.py")], cwd=EVAL, check=False, env=SCORE_ENV)
        r2 = sh([PY, str(EVAL / "test_gate.py")], cwd=EVAL, check=False, env=SCORE_ENV) if r1.returncode == 0 else r1
        log = f"[prepare.py rc={r1.returncode}]\n{r1.stdout}{r1.stderr}\n[test_gate.py rc={r2.returncode}]\n{r2.stdout}{r2.stderr}"
        (OUT / "02_pilot" / "test_gate.log").write_text(log)
        if r1.returncode == 0 and r2.returncode == 0:
            return
        if attempt < BUDGET["max_crash_repairs"]:
            repair(OUT / "02_pilot", f"cd {EVAL} && python prepare.py && python test_gate.py", log, attempt + 1)
    raise RuntimeError("prepare.py 또는 test_gate.py 3단계 점검을 통과하지 못함")


def pilot_runs():
    pilot = OUT / "02_pilot"
    if "A" in TRACKS:
        cases = {p.name: p / "run.sh" for p in sorted(pilot.iterdir()) if (p / "run.sh").exists()}
    else:  # ponytail: 트랙 B 파일럿은 현재 코드 측정만. 최적화 후보는 Phase 3 루프에서 시도한다
        cases = {"current_code": EVAL / "run_bench.sh"}
    rows = {name: run_case(pilot / name, script) for name, script in cases.items()}
    table = "| case | status | guard_pass | primary_metric | wall_time_sec |\n|---|---|---|---|---|\n" + "".join(
        f"| {k} | {v['status']} | {v.get('guard_pass')} | {v.get('primary_metric')} | {v.get('wall_time_sec')} |\n"
        for k, v in rows.items())
    (pilot / "pilot_table.md").write_text(table)
    return rows, table


def phase_pilot():
    agent("builder", STAGES["phase2_pilot"]["builder"], prompt(
        "phase2_build.md", track=",".join(TRACKS), spec=SPEC_TEXT, recon=read(OUT / "01_recon.md"),
        selected=read(OUT / "01b_ideation" / "01b_selected.json"), eval_dir=EVAL, pilot_dir=OUT / "02_pilot"),
        OUT / "02_pilot" / "builder.md", "full", env=BUILD_ENV)
    shutil.rmtree(SELFTEST, ignore_errors=True)         # 자기 점검용 정답은 남기지 않는다
    harness_check()
    rows, table = pilot_runs()
    if "A" in TRACKS:   # 트랙 A는 선별 후보 중 하나 이상이 guard를 통과해야 한다. 기준선은 세지 않는다
        base_ids = {b.get("id") for b in json.loads(read(OUT / "01b_ideation" / "01b_selected.json", "{}"))
                    .get("sota_baselines") or []}
        ok, why = any(v.get("guard_pass") for k, v in rows.items() if k not in base_ids), \
            "선별 후보 중 guard를 통과한 것이 없음 (기준선은 세지 않음)"
    else:
        ok, why = any(v.get("guard_pass") for v in rows.values()), "guard 통과 0건"
    if not ok:
        raise Stop(f"파일럿 신호 없음: {why}\n{table}")


def phase_gate_pilot():
    while True:
        say(f"[{RUN_ID}] 파일럿 완료. 채점기 확인이 필요합니다.\n\n{read(OUT / '02_pilot' / 'pilot_table.md')}\n"
            f"파일럿 판정 기준: {(CFG.get('falsification') or {}).get('pilot_check', '-')}\n\n"
            f"metrics.py\n```\n{read(EVAL / 'metrics.py')}\n```")
        a = ask("Enter 또는 y: 잠그고 본 실험 | q: 종료 | 그 밖의 글: 평가 코드 수정 지시 > ")
        if a.lower() in ("", "y"):
            lock_eval()
            git("add", "-A")                             # 파일럿 코드를 기준 커밋으로 (Phase 3의 reset·clean이 지우지 않게)
            git("commit", "--allow-empty", "-q", "-m", "pilot: 평가 하네스 승인 시점")
            STATE["best_commit"] = git("rev-parse", "HEAD")
            save_state()
            return
        if a.lower() == "q":
            raise Stop("사용자가 파일럿 확인 단계에서 종료", by_user=True)
        agent("builder", STAGES["phase2_pilot"]["builder"], prompt("phase2_fix.md", instruction=a, eval_dir=EVAL),
              OUT / "02_pilot" / f"fix_{time.strftime('%H%M%S')}.md", "full", env=BUILD_ENV)
        shutil.rmtree(SELFTEST, ignore_errors=True)
        harness_check()
        pilot_runs()


# ---- Phase 3 트랙 A/B: 시도 1회 = 에이전트 수정 → 제어기 실행·채점 → 채택(commit) 또는 원복(reset)
def phase_track_ab(track):
    st = STAGES[f"phase3_track_{track.lower()}"]
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
        T["best_metric"] = s["primary_metric"]
        row(0, STATE["best_commit"][:9], "keep", s, None, "수정 전 기준 측정")
        save_state()

    # 트랙 B는 선별 후보를 유망한 순서(selector가 정한 순서)대로 시도마다 하나씩 배정한다
    cands = (json.loads(read(OUT / "01b_ideation" / "01b_selected.json", "{}")).get("selected_candidates") or []) \
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
            selected=read(OUT / "01b_ideation" / "01b_selected.json"), pilot=read(OUT / "02_pilot" / "pilot_table.md"),
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
                T.setdefault("cases", {}).update({k: {**v, "attempt": f"A{it}"} for k, v in s["cases"].items()})
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


# ---- Phase 3 트랙 C: 증명 ↔ 반박·반례 탐색을 라운드로 반복
def run_hunter(rd):
    script = rd / "counterexample_hunter.py"
    for attempt in range(BUDGET["max_crash_repairs"] + 1):
        r = sh([PY, str(script)], cwd=rd, check=False) if script.exists() else None
        log = f"{r.stdout}{r.stderr}" if r else "counterexample_hunter.py 없음"
        (rd / "hunter.log").write_text(log)
        j = last_json(r.stdout) if r and r.returncode == 0 else None
        if j:
            return {k: j.get(k) for k in ("tested_cases", "counterexamples_found", "worst_counterexample", "tightness_ok")}
        if attempt < BUDGET["max_crash_repairs"]:
            repair(rd, f"cd {rd} && python counterexample_hunter.py", log, attempt + 1)
    return {"tested_cases": 0, "counterexamples_found": None, "worst_counterexample": None, "tightness_ok": False}


def phase_track_c():
    st = STAGES["phase3_track_c"]
    C = STATE.setdefault("track_C", {"round": 0, "verdict": None})
    while C["round"] < BUDGET["max_debate_rounds"]:
        C["round"] += 1
        n = C["round"]
        save_state()
        rd, prev = MAIN / f"C_round_{n:02d}", MAIN / f"C_round_{n - 1:02d}"
        proof = agent("C_prover", st["prover"], prompt(
            "track_c_prove.md", n=n, spec=SPEC_TEXT, selected=read(OUT / "01b_ideation" / "01b_selected.json"),
            prev_proof=read(prev / "proof.md", "없음 (첫 라운드)"), objections=read(prev / "refute.md", "없음"),
            verdict=json.dumps(C["verdict"], ensure_ascii=False) if C["verdict"] else "없음"),
            rd / "proof.md", "readonly")
        try:
            _, logic = agent_json("C_refuter", st["refuter"], prompt(
                "track_c_refute.md", n=n, spec=SPEC_TEXT, proof=proof, round_dir=rd),
                rd / "refute.md", "full", parse_verdict, VERDICT_SCHEMA)
        except ValueError:
            logic = {"logic_sound": False, "fatal_flaws": ["반박 모델의 판정 JSON을 다시 요청해도 받지 못함"]}
        v = {**run_hunter(rd), **{k: logic.get(k) for k in ("logic_sound", "scope_realistic", "non_trivial", "fatal_flaws")}}
        (MAIN / f"round_{n}_verdict.json").write_text(json.dumps(v, ensure_ascii=False, indent=2))
        C["verdict"] = v
        save_state()
        if v["logic_sound"] and v["counterexamples_found"] == 0 and (v["tested_cases"] or 0) > 0 \
                and v["scope_realistic"] and v["non_trivial"] and v["tightness_ok"]:
            return
    v = C["verdict"]
    if not v["logic_sound"] or v["counterexamples_found"] != 0 or not v["tested_cases"]:
        raise Stop(f"트랙 C 기각: 논리 결함 또는 수치 반례\n{json.dumps(v, ensure_ascii=False)}")
    last = MAIN / f"C_round_{C['round']:02d}"
    say(f"[{RUN_ID}] 트랙 C: 논리와 수치 반례 검사는 통과했지만 일반성·비자명성·밀착도에서 이견이 남음.\n"
        f"판정: {json.dumps(v, ensure_ascii=False)}\n\n증명 앞부분:\n{read(last / 'proof.md')[:1500]}\n\n"
        f"반박 요지:\n{read(last / 'refute.md')[-1500:]}")
    if ask("Enter: Phase 4 감사 진행 | q: 폐기하고 종료 > ").lower() == "q":
        raise Stop("트랙 C 결과를 사용자가 폐기", by_user=True)


# ---- Phase 4: 다른 계열 모델의 감사 → 보고서
def phase_audit():
    st = STAGES["phase4_audit"]
    files = sorted(str(p.relative_to(REPO)) for p in OUT.rglob("*")
                   if p.is_file() and p.suffix in (".json", ".tsv", ".csv", ".log", ".md", ".py", ".sh"))
    verdicts = "\n".join(f"{p.name}: {p.read_text()}" for p in sorted(MAIN.glob("round_*_verdict.json"))) or "없음"
    ctx = dict(spec=SPEC_TEXT, tracks=",".join(TRACKS), files="\n".join(files[:400]), results=tail(MAIN / "results.tsv", 80),
               verdicts=verdicts, selected=read(OUT / "01b_ideation" / "01b_selected.json"), out_dir=OUT)
    texts = parallel([(s, prompt("phase4_audit.md", **ctx), OUT / "04_critique" / f"auditor_{i:02d}.md", "readonly", True)
                      for i, s in enumerate(st["auditors"], 1)])
    (OUT / "04_critique.md").write_text("\n\n".join(
        f"# auditor_{i:02d} ({s['provider']}/{s['model']}/{s['effort']})\n\n{t}"
        for i, (s, t) in enumerate(zip(st["auditors"], texts), 1)))
    verify_locks()
    agent("reporter", st["reporter"], prompt("phase4_report.md", **ctx, critique=read(OUT / "04_critique.md"),
                                             notes=tail(MAIN / "notes.md", 80), env=read(OUT / "env.txt")[:3000]),
          OUT / "05_report.md", "readonly", human=True)


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
    rep = read(OUT / "05_report.md")
    heads = [m.start() for m in re.finditer(r"^## ", rep, re.M)]   # 알림에는 결론 절(첫 절)까지만 보낸다
    say(f"[{RUN_ID}] 완료\n보고서: {OUT / '05_report.md'}\n\n{(rep[:heads[1]] if len(heads) > 1 else rep[:3000]).strip()}")
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
