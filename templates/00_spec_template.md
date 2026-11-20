# 연구 명세: <연구 제목>

```yaml
# 실행 때 이 블록의 값이 그대로 쓰인다. 다른 곳에서 덮어쓰지 않는다.
track: "A"                        # A: 효용 실증 | B: 코드 최적화 | C: 정리 증명. 하나만 적는다 (여러 트랙은 실행을 나눈다)
source_repo: "<원본 프로젝트 절대경로>"
base_branch: "main"               # 클론할 기준 브랜치. 원본의 이 브랜치는 건드리지 않는다
base_env: "<conda env 이름 또는 python 절대경로>"   # 실행 전용 venv에서 이 env의 패키지를 그대로 쓸 수 있다
setup_cmds: []                    # venv 생성 뒤 클론 폴더에서 한 번 실행 (예: "pip install -e . --no-build-isolation")
build_cmd: ""                     # 소스 수정 뒤 채점 전에 매번 실행 (C++/HIP 확장 재빌드 등). 없으면 빈 문자열
copy_inputs: []                   # 클론에 없는 입력(원본에서 git-ignored인 것, 예: "data/"). 클론으로 복사한다
references: []                    # (선택) 선행연구 조사 단계에서 먼저 읽을 자료: 원본 기준 경로(PDF, 폴더)나 URL, 논문 제목.
                                  #   경로로 적은 자료는 클론의 .ar_refs/로 복사하고 push하지 않는다
push_remote: "origin"             # 원본 저장소의 원격. 보고서를 확인한 뒤 새 브랜치를 여기로 push한다. 공개 저장소면 push하지 않는다
workspace:
  focus_hint: []                  # 먼저 볼 모듈
constraints:
  timing_rule: "single"           # 실행 한 번. "second_pass"면 두 번 돌려 두 번째 시간만 쓴다(첫 실행에 컴파일·캐시 비용이 섞일 때)
  split_lock: []                  # 읽기 전용으로 잠글 분할·시드 파일 (클론 기준 상대경로)
evaluation_contract:
  guard: "<깨지면 안 되는 조건: 수치 발산 없음, 정확도 기준 등>"
  metric: "<비교하거나 개선할 지표 이름과 단위>"
  direction: "higher_is_better"   # 또는 lower_is_better
  min_improvement_pct: 1.0        # 트랙 B: 이보다 작은 개선은 측정 잡음으로 보고 버린다
budget:                           # 쓰는 트랙의 항목만 있으면 된다. 다른 트랙 항목은 지워도 된다
  max_iterations: 15              # (트랙 A/B) 시도 횟수 상한
  max_crash_repairs: 3            # (공통) 비정상 종료 시 자동 수리 횟수
  max_ideation_rounds: 1          # (공통, 생략하면 1) Phase 2 아이디어 토론의 최대 회차. 1이면 제안 한 번, 비평 한 번.
                                  #   N이면 보완 요청이 남지 않을 때까지 최대 N회 주고받고, N회차 비평은 채택·탈락만 낸다.
                                  #   호출은 많아야 N × 2 × (diverger 수)회
  max_debate_rounds: 3            # (트랙 C) 증명-반박 라운드
  run_limits: {codex: 6}          # (공통) 실행 1회당 호출 상한 (키: 모델 별명 또는 벤더). 닿으면 슬롯의 after_quota → 벤더 대체 모델.
                                  #   한도가 바닥나면 상한과 상관없이 대체 모델로 넘어간다. 한도가 작은 벤더만 적는다
  seeds: 3                        # (트랙 A) 시도마다 run.sh를 AR_SEED=0..seeds-1로 돌려 평균과 표준오차를 낸다
  bench_rounds: 3                 # (트랙 B) 수정 전과 수정 후를 번갈아 잴 횟수. 중앙값으로 비교한다
  call_timeout_min: null          # 모델 호출 한 번의 시간 제한(분). null이면 제한 없음
  run_timeout_min: null           # (트랙 A/B) 실험 스크립트 1회의 시간 상한(분). 넘기면 끊고 비정상 종료로 처리. null이면 상한 없음
stages:                           # 단계별 모델 배정. 쓰는 트랙의 단계는 모두 있어야 한다
                                  # 이 표에 벤더가 하나뿐이면 아래 '다른 벤더' 규칙을 '다른 모델'로 낮춰 적용한다
                                  # 기본 배정: fable은 한 번만 부르는 결정적 단계, opus는 반복 루프와 글쓰기,
                                  #   codex는 몇 번만 부르는 비평·반박·감사, agy는 대조와 검증(지어내지 않는 쪽이 강하다)
  phase1_recon:
    slots:                        # role마다 prompts/phase1_<role>.md
      - {provider: claude, model: opus, effort: medium, role: repo_scan}
      - {provider: claude, model: fable, effort: high, role: lit_search}   # 트랙 A·C: 세 벤더가 따로 조사한 것을 합치고
                                                                        #   검증한다. 트랙 B: 혼자 조사한다
  phase2_ideation:
    divergers:                    # 아이디어를 내는 모델. 같은 모델을 여러 줄 넣을 때는 lens: "통계물리 관점"처럼 관점을 붙인다
      - {provider: claude, model: fable, effort: high}
      - {provider: claude, model: opus, effort: xhigh}
      - {provider: agy, model: neon, effort: high}
    critics:                      # diverger마다 한 명씩 1:1로 짝지어 토론한다. 다른 벤더를 먼저 짝짓는다.
      - {provider: codex, model: astra, effort: high}  # critic이 채택·탈락을 정하므로 diverger보다 약하지 않게 둔다
      - {provider: agy, model: neon, effort: xhigh}
      - {provider: claude, model: opus, effort: xhigh}
    selector: {provider: claude, model: fable, effort: high}
  phase3_pilot:
    builder: {provider: claude, model: fable, effort: high}   # 채점 코드를 쓴다. 틀리면 뒤 단계가 모두 틀리므로 강하게 둔다
    explainer: {provider: agy, model: neon, effort: medium}  # 파일럿 관문에서 채점 코드가 하는 일을 자연어로 설명. builder와 다른 벤더 권장
  crash_repair: {provider: claude, model: opus, effort: high}
  phase4_track_a:
    experimenter: {provider: claude, model: opus, effort: xhigh}
  phase4_track_b:
    worker: {provider: claude, model: opus, effort: high}
    unstuck_peer: {provider: agy, model: neon, effort: xhigh}
  phase4_track_c:
    prover: {provider: claude, model: fable, effort: high,
             after_quota: {provider: claude, model: opus, effort: xhigh}}
    refuter: {provider: codex, model: astra, effort: high,
              after_quota: {provider: agy, model: neon, effort: xhigh}}
  phase5_audit:
    auditors:                     # 본 실험(Phase 4) 주력 모델과 다른 벤더만
      - {provider: agy, model: neon, effort: high}
      - {provider: codex, model: astra, effort: high}
    reporter: {provider: claude, model: opus, effort: xhigh}
    checker: {provider: agy, model: neon, effort: high}      # 보고서의 수치·결론을 결과 파일과 대조해 틀린 문장만 고친다. reporter와 다른 벤더
falsification:
  pilot_check: "<파일럿에서 볼 최소 신호: 단순 기준선 대비 차이, 수치 발산 없음 등>"
```

## 1. 연구 질문과 핵심 가설

## 2. 기대하는 접근 방향과 허용하는 가정의 범위

## 3. 이 프로젝트에서 지켜야 할 규칙 (해당 시)
