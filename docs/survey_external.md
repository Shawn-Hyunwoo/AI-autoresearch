# 외부 자율 연구 도구 조사

2026-11-20에 공개 레포 6개를 shallow clone해 README와 핵심 코드를 읽고 이 도구와 비교했다. 클론한 코드는 실행하지 않았다. 레포마다 이 도구에 없는 기법만 적고, 이미 있는 기능은 뺐다. 마지막 표가 반영 후보다. "종류"는 구조(제어기가 하는 일)인지 규칙(에이전트에게 거는 지시)인지를 뜻한다.

## 1. karpathy/autoresearch

- 지침 파일 `program.md` 하나와 `train.py`, `prepare.py`가 전부다. 에이전트가 `train.py`만 고치고, 5분 고정 학습 뒤 `val_bpb`가 좋아지면 커밋을 남기고 아니면 `git reset` 한다.
- 결과는 `results.tsv`(commit, 지표, 메모리, keep/discard/crash, 설명)에 쌓는다. 사람이 멈출 때까지 끝없이 돈다.
- 제어기가 없다. 에이전트가 직접 실행하고 채점을 읽는다.

이 도구에 없는 것:
- **단순성 기준** (`program.md`, Simplicity criterion): 개선 폭이 비슷하면 코드가 단순한 쪽을 남긴다. 코드를 지우고 성능이 같으면 그것도 채택한다. 규칙.
- **실험 1회 시간 상한** (`program.md`, Timeout): 10분을 넘긴 실행은 끊고 실패로 기록한다. 이 도구는 모델 호출에는 선택적 상한(`call_timeout_min`)이 있지만, 실험 스크립트를 돌릴 때는 상한이 없다. 구조.

## 2. uditgoenka/autoresearch

- Claude Code 플러그인 하나다. `.claude/skills/autoresearch/SKILL.md`가 모드를 고르고, 하위 명령 14개의 절차는 `.claude/commands/autoresearch/*.md`에 있다.
- 루프는 프롬프트로만 돈다. 결정적 코드는 `scripts/orchestrate.sh`(정체 판정, 명령 검사) 하나다.
- `scripts/transform.sh`가 같은 내용을 Codex, OpenCode 등 다른 에이전트 형식으로 옮긴다.

이 도구에 없는 것:
- **fork-and-flush** (`references/fork-and-flush.md`)
  - 시도 예산의 1/3과 2/3 지점에서 한 번씩 돈다. 5회 연속 채택이 없으면 그보다 일찍 돈다.
  - 최고 커밋을 표시해 두고 새 에이전트 4개를 각자 worktree에서 띄운다. 이들은 계획서, `results.tsv`, git log만 받고 리더의 가설은 받지 않는다.
  - 절반은 지시 없이 자유롭게, 절반은 기록에 없는 실험을 하나씩 배정받아 시도한다. 이긴 가지가 다음 리더가 된다.
  - 구조.
- **위험 명령 차단 훅** (`.claude/hooks/autoresearch/dangerous-cmd-block.cjs`, `scout-block.cjs`, `privacy-block.cjs`)
  - 강제 push, `rm -rf`, `reset --hard`, `clean -f`를 막는다. `.ssh/`, `.env` 같은 경로를 읽는 것도 막는다.
  - 훅이 실패하면 막지 않고 통과시킨다.
  - 구조.
- **채점 불가 회차는 정체로 세지 않음** (`scripts/orchestrate.sh` plateau): 점수를 낼 수 없었던 회차는 "개선 없음"과 따로 센다. 이런 회차가 5회 이어지면 BLOCKED로 멈춘다. 구조.

## 3. aiming-lab/AutoResearchClaw

- 주제 선정부터 문헌, 가설, 실험 설계, 코드, 실행, 분석, 집필, 검토, 인용 확인까지 23단계를 차례로 돈다(`researchclaw/pipeline/stages.py`, `runner.py`).
- 실험은 local, Docker, SSH, Colab 중 하나에서 돈다(`researchclaw/experiment/*_sandbox.py`).
- 사람 개입은 `researchclaw/hitl/`, 실행 간 교훈은 `researchclaw/evolution.py`와 `metaclaw_bridge/`가 맡는다. 연결되지 않은 모듈도 꽤 있다.

이 도구에 없는 것:
- **인용 확인의 DataCite 대체 조회와 제목 대조** (`researchclaw/literature/verify.py`)
  - Crossref에 없는 DOI는 DataCite에서 다시 찾는다.
  - 인용한 제목과 찾은 제목의 단어 겹침이 0.8 이상이면 확인됨, 0.5~0.8이면 의심, 0.5 미만이면 지어낸 것으로 본다.
  - ID나 DOI는 실제로 있지만 다른 논문에 붙은 경우를 잡는다. 구조.
- **본문 수치 대조** (`researchclaw/pipeline/paper_verifier.py`, `verified_registry.py`)
  - 실험 출력 파일의 모든 수치로 목록을 만든다. 반올림한 꼴과 ×100, ÷100 한 꼴도 넣는다.
  - 결과 절과 표의 수치를 이 목록과 1% 안에서 맞춰 본다. 목록에 없는 수치가 있으면 원고를 통과시키지 않는다.
  - 구조.
- **Docker 샌드박스** (`researchclaw/experiment/docker_sandbox.py`, `docker/entrypoint.sh`)
  - 실행마다 컨테이너를 새로 띄우고, 작업 폴더만 연결한다. 메모리 상한을 둔다.
  - 네트워크는 설치 단계에서만 열고, 본 실행 전에 iptables로 끊는다.
  - 실행 전에 코드의 구문 트리를 훑어 subprocess, socket, eval 호출을 막는다.
  - 구조.
- **사람 개입 모드 6종** (`researchclaw/hitl/config.py`, `presets.py`)
  - full-auto, gate-only, checkpoint, step-by-step, co-pilot, custom이 있다. 단계마다 정책 필드가 11개다.
  - 품질 점수가 0.7 아래면 멈추는 기능(`hitl/smart_pause.py`)이 있다.
  - 비용이 예산의 50, 80, 100%에 닿으면 멈추는 기능(`hitl/cost_guard.py`)이 있다.
  - 확인 단계에 답이 없으면 Slack이나 메일로 다시 알린다(`hitl/escalation.py`).
  - 구조.
- **교훈 축적** (`researchclaw/evolution.py`, `metaclaw_bridge/lesson_to_skill.py`)
  - 실패한 단계, 방향 전환, NaN 경고 같은 사건을 JSONL 한 줄(단계, 분류, 심각도, 300자 설명)로 남긴다.
  - 다음 단계 프롬프트에 위 5개를 넣는다. 30일 반감기로 가중한다.
  - 실행 간에 실제로 이어지는 것은 LLM이 교훈을 스킬 파일로 바꾼 것뿐이다. 이 스킬이 결과에 도움이 됐는지 보고 걸러 내는 단계는 없다.
  - 구조.
- **실패 원인 분류 뒤 수리** (`researchclaw/pipeline/experiment_diagnosis.py`): 실험이 죽으면 원인을 정해진 분류로 먼저 나누고 고친다. 구조.

## 4. RUC-NLPIR/Arbor

- 무엇을 시도할지 정하는 Coordinator가 가설 트리를 관리하고 코드는 쓰지 않는다. Executor가 노드 하나를 각자 git worktree에서 구현한다(`src/coordinator/orchestrator.py`, `src/coordinator/tools/executor_run.py`).
- 한 주기는 이렇게 돈다: 자식 노드를 제안하고, 하나를 골라 실행하고, dev 점수를 매기고, 교훈을 위로 올리고, 병합하거나 가지를 친다.
- 트리는 JSON과 그 마크다운 사본으로 저장한다(`src/coordinator/idea_tree.py`).

이 도구에 없는 것:
- **가설 트리와 교훈 전파** (`src/coordinator/idea_tree.py`, `src/coordinator/tools/tree_ops.py` `propagate_insights`)
  - 실험이 끝날 때마다 부모에서 뿌리까지 올라가며, 각 조상의 insight를 자식들의 insight로 LLM이 다시 쓴다.
  - 가지를 친 노드는 "[Pruned: 이유]"를 남긴다.
  - 다음 아이디어 제안 때 "PRUNED LESSONS"를 보여 주고, 같은 숨은 가정을 가진 아이디어를 다시 내려면 그 교훈을 어떻게 피하는지 말하게 한다.
  - 구조와 규칙.
- **실험마다 worktree** (`src/coordinator/tools/worktree.py`, `src/core/git_artifacts.py`): 노드마다 브랜치와 임시 worktree를 만든다. 끝나면 생성물을 뺀 나머지를 커밋하고 worktree만 지운다. 병렬 실행이 가능하다. 구조.
- **정체 단계 판정** (`src/coordinator/convergence.py`): 개선 없는 실행이 3회면 경고, 5회면 접근을 바꾸라고 요구하고, 8회면 멈춘다. 구조와 규칙.
- **실행 간 교훈** (`src/experience.py`, `src/distill.py`, `src/recall.py`)
  - 실행 중에 "leverage" 또는 "pitfall" 표시를 붙인 발견을 남긴다.
  - 끝나면 `EXPERIENCE.md`로 정리한다.
  - 다음 실행이 키워드로 골라 쓰는데, 쓰기 전에 사용자에게 묻는다.
  - 구조.

주의할 점은 두 가지다. 루프 도중 병합할 때마다 test 점수를 보므로 test 세트가 실제로는 held-out이 아니다. 교훈 전파는 조상의 글을 덮어써서 원래 내용이 남지 않는다.

## 5. OpenNSWM-Lab/FAROS

- FastAPI 백엔드와 React 화면으로 이루어져 있다. 아이디어, 계획, 코드, 실험, 논문, 검토(ReviewX)가 공용 pydantic 계약(`backend/app/contracts/scientific_research.py`)으로 이어진다.
- 검토마다 사람이 다음 단계를 정한다. ReviewX(`backend/app/modules/review/`)는 약 1만 2천 줄이고 대부분 정규식과 규칙 검사다.

이 도구에 없는 것:
- **주장-증거 연결표** (`contracts/scientific_research.py` ClaimBinding, `modules/review/claim_extractor.py`, `evidence_verifier.py`)
  - 원고에서 주장 문장을 정규식으로 뽑아, 증거 ID와 지표를 하나씩 연결한다.
  - 판정은 supported, weakly_supported, unsupported, contradicted, artifact_absent, needs_human 중 하나다.
  - 없는 증거 ID를 가리키는 연결은 거부한다.
  - 구조.
- **검토 결과를 수정 항목으로 변환** (`modules/review/revision_planner.py`, `revision_feedback.py`): 지적마다 고칠 모듈과 완료 조건을 붙이고, pending, resolved, verified로 상태를 추적한다. 구조.
- **계획 변경 기록** (`modules/review/plan_delta.py`): 바뀐 필드마다 {before, after, 이유, 근거 ID}를 남기고, 전체를 sha256으로 봉인한다. 구조.
- **승인 무효화** (`human_feedback_verification.py`, `audit_chain.py`): 사람의 승인을 산출물 해시와 묶어 둔다. 산출물이 바뀌면 승인이 다시 대기 상태로 돌아간다. 구조.

주의할 점은 셋이다. 주장과 증거를 키워드와 값의 겹침으로 연결해서 잘못 연결될 수 있다. 수치 허용 오차가 8%로 느슨하다. 계획은 승인 뒤에도 잠기지 않는다.

## 6. K-Dense-AI/scientific-agent-skills

- 과학용 에이전트 스킬 약 177개를 모은 레포다. 생명과학 쪽 비중이 크다.
- 읽은 스킬은 paper-lookup, citation-management, literature-review, hypothesis-generation, scientific-writing, peer-review, scientific-critical-thinking, uncertainty-and-units, qiskit 테스트 문서다.

이 도구에 없는 것:
- **arXiv와 DOI 조회의 함정** (`skills/paper-lookup/references/arxiv.md`, `skills/citation-management/references/citation_validation.md`)
  - arXiv가 붙이는 DOI(`10.48550/arXiv.<번호>`)는 DataCite DOI라 Crossref에서 404가 난다.
  - arXiv API는 오류도 HTTP 200으로 돌려준다.
  - 이 도구는 Crossref만 보므로 실제 arXiv DOI를 "없음"으로 표시한다. 2026-11-20에 `10.48550/arXiv.1706.03762`로 직접 확인했다(Crossref 404, DataCite에서는 제목까지 조회됨).
  - 구조.
- **찾지 못함과 없음의 구분** (`skills/hypothesis-generation/references/literature_search_strategies.md`): 조사 범위를 기록하고, "선행 연구 없음"이 아니라 "조사 범위에서 찾지 못함"이라고 쓴다. 규칙.
- **아이디어마다 반증 조건** (`skills/hypothesis-generation/assets/falsification_controls_template.json`, `prediction_rival_matrix_template.csv`): 가설마다 어떤 결과가 나오면 버리는지, 경쟁 가설은 무엇을 예측하는지 적는다. 규칙.
- **불확실성 표기** (`skills/uncertainty-and-units/references/reporting-rules.md`): 불확실성을 유효숫자 1~2자리로 먼저 반올림하고, 값을 같은 자리에서 반올림한다. ±가 표준편차인지 표준오차인지 신뢰구간인지 적는다. 규칙.

## 반영 후보

이 도구에 이미 있는 기능은 넣지 않았다. 위에서부터 들인 품에 비해 얻는 것이 큰 순서다.

| 기법 | 출처 | 고칠 위치 | 작업량 | 종류 | 얻는 것 |
|---|---|---|---|---|---|
| Crossref에 없는 DOI를 DataCite에서 다시 조회 | AutoResearchClaw, K-Dense | `run_research.py` `check_citations` | 소 | 구조 | arXiv DOI를 "없음"으로 잘못 표시하던 결함이 사라진다 |
| 실험 1회 시간 상한 (`budget.run_timeout_min`, 선택) | karpathy | `run_research.py` `run_case`(실행 호출에 timeout), 계획서 템플릿 | 소 | 구조 | 실험 하나가 멈춰도 실행 전체가 서지 않고 crash로 넘어간다 |
| 보고서 수치를 결과 파일 수치 목록과 대조해, 목록에 없는 수치를 checker에게 넘김 | AutoResearchClaw | `run_research.py` `check_report`, `prompts/phase5_check.md` | 소~중 | 구조 | 결과 파일에 없는 수치가 결정적으로 드러난다. checker의 눈에만 기대지 않는다 |
| 단순성 기준: 트랙 B에서 지표가 측정 잡음 안에서 같고 코드가 줄면 채택 | karpathy | `prompts/track_b.md`, `phase_track_ab` 채택 조건 | 소 | 규칙+구조 | 최적화 결과물이 덜 복잡해진다 |
| 아이디어마다 반증 조건 한 줄 (`kill_if`) | K-Dense, FAROS | `prompts/phase2_diverge.md`, `phase2_critic.md`, `SELECT_SCHEMA` | 소 | 규칙 | 파일럿 확인 단계와 감사에서 "무엇이 나오면 실패인가"가 미리 정해져 있다 |
| "알려지지 않음"을 "조사 범위에서 찾지 못함"으로 | K-Dense | `prompts/phase1_lit_synth.md` | 소 | 규칙 | 새로움 주장이 조사 범위를 넘지 않는다 |
| 프로젝트별 교훈 파일: 실행이 끝나면 버려진 시도와 이유, 치명 지적과 판단을 원문 그대로 `~/autoresearch_runs/<프로젝트>/lessons.md`에 덧붙이고, 다음 실행의 조사·아이디어 단계에 넘김 | Arbor, AutoResearchClaw | `phase_final`, `phase_recon`, `phase2_diverge.md` | 중 | 구조 | 같은 프로젝트에서 실패한 아이디어를 다시 시도하지 않는다. 요약하지 않고 원문을 남겨야 Arbor처럼 내용이 흐려지지 않는다 |
| 코드 수정 단계를 컨테이너에서 실행 | AutoResearchClaw | `llm_router.py`의 full 모드 호출, 설치 문서 | 대 | 구조 | 모든 권한으로 도는 에이전트가 클론 밖을 건드리지 못한다. 지금은 클론 격리와 프롬프트 지시뿐이다 |

넣지 않기로 한 것:
- **fork-and-flush의 병렬 가지와 Arbor의 가설 트리:** 이 도구의 시도는 이미 매번 앞 대화 없이 요약만 받는 새 호출이라 맥락을 비우는 효과가 있다. 병렬 worktree와 트리 관리는 코드가 크게 늘어난다.
- **AutoResearchClaw의 개입 모드 6종, 품질 점수 정지, 비용 단계 정지:** 설정이 늘어난다. 확인 단계 4개와 `run_limits`로 충분하다.
- **FAROS의 주장-증거 연결표, 계획 변경 봉인, 승인 해시 체인:** 1만 줄 규모이고, 연결을 키워드 겹침으로 하는 한계가 있다. 수치 대조(위 표 셋째 줄)가 같은 목적을 더 작게 이룬다.
- **인용 제목 단어 겹침 판정:** 이 도구의 조사 보고는 인용 제목을 정해진 칸에 적지 않아서 비교할 원문을 뽑기 어렵다. 찾은 제목은 이미 표로 정리 모델에게 넘긴다.
- **위험 명령 차단 훅:** claude에만 걸리고 agy, codex에는 걸리지 않는다. 막으려면 컨테이너(위 표 마지막 줄)가 맞다.
- **정체 단계 판정(3·5·8회), 불확실성 반올림 규칙, 관련도 LLM 채점과 참고문헌 자동 삭제:** 규칙만 늘고 얻는 것이 적다.
