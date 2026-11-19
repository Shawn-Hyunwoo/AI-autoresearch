# 연구 명세: <연구 제목>

```yaml
# 실행 때 이 블록의 값이 그대로 쓰인다. 다른 곳에서 덮어쓰지 않는다.
track: "A"                        # A: 효용 실증 | B: 코드 최적화 | C: 정리 증명. 여러 개면 "A,C" (적은 순서대로 실행)
source_repo: "<원본 프로젝트 절대경로>"
base_branch: "main"               # 클론할 기준 브랜치. 원본의 이 브랜치는 건드리지 않는다
base_env: "<conda env 이름 또는 python 절대경로>"   # 실행 전용 venv가 이 env의 패키지를 그대로 본다
setup_cmds: []                    # venv 생성 뒤 클론 폴더에서 한 번 실행 (예: "pip install -e . --no-build-isolation")
build_cmd: ""                     # 소스 수정 뒤 채점 전에 매번 실행 (C++/HIP 확장 재빌드 등). 없으면 빈 문자열
copy_inputs: []                   # 원본에서 git-ignored 상태라 클론에 없는 입력 (예: "data/"). 복사한다
push_remote: "origin"             # 보고서 확인 뒤 새 브랜치를 push할 원본의 원격. 공개 저장소면 push하지 않는다
workspace:
  focus_hint: []                  # 먼저 볼 모듈
constraints:
  timing_rule: "second_pass"      # 두 번 실행해 두 번째 시간만 쓴다. 속도 비교가 없으면 "single"
  split_lock: []                  # 읽기 전용으로 잠글 분할·시드 파일 (클론 기준 상대경로)
evaluation_contract:
  guard: "<깨지면 안 되는 조건: 수치 발산 없음, 정확도 기준 등>"
  metric: "<비교하거나 개선할 지표 이름과 단위>"
  direction: "higher_is_better"   # 또는 lower_is_better
  min_improvement_pct: 1.0        # 트랙 B: 이보다 작은 개선은 측정 잡음으로 보고 버린다
budget:                           # 쓰는 트랙의 항목만 있으면 된다. 다른 트랙 항목은 지워도 된다
  max_iterations: 15              # (트랙 A/B) 시도 횟수 상한
  max_crash_repairs: 3            # (공통) 비정상 종료 시 자동 수리 횟수
  max_debate_rounds: 3            # (트랙 C) 증명-반박 라운드
  run_limits: {fable: 5, codex: 3}  # (공통) 실행 1회당 호출 상한 (키: 모델 별명 또는 벤더). 닿으면 슬롯의 after_quota → 벤더 대체 모델
  seeds: 3                        # (트랙 A) 시도마다 run.sh를 AR_SEED=0..seeds-1로 돌려 평균과 표준오차를 낸다
  bench_rounds: 3                 # (트랙 B) 수정 전과 수정 후를 번갈아 잴 횟수. 중앙값으로 비교한다
  call_timeout_min: null          # 모델 호출 한 번의 시간 제한(분). null이면 제한 없음
stages:                           # 단계별 모델 배정. 쓰는 트랙의 단계는 모두 있어야 한다
  phase1_recon:
    slots:                        # role마다 prompts/phase1_<role>.md
      - {provider: claude, model: opus, effort: high, role: repo_scan}
      - {provider: agy, model: neon, effort: high, role: lit_search}
  phase2_ideation:
    critics_mode: all             # all: 모든 비평자가 전 후보 | shard: 후보를 나눠 맡음
    divergers:                    # 같은 모델을 여러 줄 넣을 때는 lens: "통계물리 관점"처럼 관점을 붙인다
      - {provider: claude, model: fable, effort: high}
      - {provider: claude, model: opus, effort: xhigh}
      - {provider: agy, model: neon, effort: xhigh}
    critics:
      - {provider: agy, model: neon, effort: high}
      - {provider: codex, model: sol, effort: high}
    selector: {provider: claude, model: opus, effort: xhigh}
  phase3_pilot:
    builder: {provider: claude, model: opus, effort: high}
  crash_repair: {provider: claude, model: opus, effort: high}
  phase4_track_a:
    experimenter: {provider: claude, model: opus, effort: xhigh}
  phase4_track_b:
    worker: {provider: claude, model: opus, effort: high}
    unstuck_peer: {provider: agy, model: neon, effort: xhigh}
  phase4_track_c:
    prover: {provider: claude, model: fable, effort: xhigh,
             after_quota: {provider: claude, model: opus, effort: max}}
    refuter: {provider: codex, model: astra, effort: high,
              after_quota: {provider: agy, model: neon, effort: xhigh}}
  phase5_audit:
    auditors:                     # 본 실험(Phase 4) 주력 모델과 다른 벤더만
      - {provider: agy, model: neon, effort: high}
      - {provider: codex, model: sol, effort: high}
    reporter: {provider: claude, model: opus, effort: high}
falsification:
  pilot_check: "<파일럿에서 볼 최소 신호: 단순 기준선 대비 차이, 수치 발산 없음 등>"
```

## 1. 연구 질문과 핵심 가설

## 2. 기대하는 접근 방향과 허용하는 가정의 범위

## 3. 이 프로젝트에서 지켜야 할 규칙 (해당 시)
