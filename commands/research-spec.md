---
description: 자율 연구 실행의 명세서(00_spec.md)를 사람과 문답하며 확정한다 (Phase 0)
argument-hint: <연구 질문>
---

너는 자율 연구 도구(`~/autoresearch/`)의 Phase 0을 진행한다. 목표는 사람과 문답해 검증 가능한 연구 명세서 `00_spec.md` 하나를 확정하는 것이다. 실행은 시작하지 않는다.

연구 질문: $ARGUMENTS

먼저 읽을 것:
- `~/autoresearch/templates/00_spec_template.md` (명세서 형식과 기본 모델 배정·호출 상한)
- `~/autoresearch/providers.yaml` (쓸 수 있는 벤더, 모델 별명, 추론 단계. 명세에 옮겨 적지 않는다)
- 기본값: `<프로젝트>/.autoresearch/defaults.yaml`이 있으면 그 값으로 템플릿을 채운다. 없으면 `~/autoresearch_runs/<프로젝트명>/*/00_spec.md` 중 가장 최근 것의 값을 쓴다. 둘 다 없으면 템플릿 값 그대로다.
- `~/autoresearch/prompts/_writing_rules.md` (질문과 설명에 적용할 서술 규칙)
- 지금 폴더가 git 저장소면 그 저장소의 CLAUDE.md 또는 AGENTS.md

순서:
1. 연구 질문을 한두 문장으로 다시 말하고, 트랙(A 효용 실증, B 코드 최적화, C 정리 증명)을 제안한다. 근거를 한 줄 붙인다.
2. 아래 항목을 한 번에 두세 개씩 묻는다. 저장소에서 알 수 있는 것은 직접 확인해 기본값으로 제시하고 확인만 받는다.
   - source_repo(기본: 지금 저장소의 최상위 경로), base_branch(기본: 지금 브랜치), base_env, setup_cmds, build_cmd
   - copy_inputs: 실험에 필요한데 git-ignored라 클론에 따라오지 않는 입력. `git status --ignored`로 후보를 보여 준다
   - evaluation_contract: guard(깨지면 안 되는 조건), metric(지표와 방향), 트랙 B면 min_improvement_pct
   - budget(시도 횟수, 수리 횟수, 증명 라운드, 호출 상한)과 push_remote
   - 단계별 모델 배정(`stages`)과 호출 상한(`budget.run_limits`): 기본값을 표로 보여 주고 바꿀 것을 묻는다. 명세에는 쓰는 트랙의 단계를 빠짐없이 적는다(실행 때 명세의 값이 그대로 쓰이고 다른 곳에서 채워지지 않는다). 모델과 추론 단계는 providers.yaml에 있는 것만 쓴다. 트랙 C의 prover와 refuter는 다른 벤더, Phase 4 auditors에는 Phase 3 주력 모델의 벤더가 없어야 한다
   - 이 프로젝트에서 계속 쓸 값이라고 하면 `<프로젝트>/.autoresearch/defaults.yaml`에 적을지 묻는다(같은 키 구조, 늘 같은 것만)
3. 연구 질문, 가설, 허용 가정을 명세 본문에 평문으로 정리한다. guard와 metric이 실제로 계산할 수 있는 형태인지 확인한다. 계산 방법이 모호하면 묻는다.
4. 확정되면 `~/autoresearch_runs/<프로젝트명>/<YYYY-MM-DD>_<짧은_영문_주제>/00_spec.md`에 쓴다. 프로젝트명은 source_repo 폴더 이름이다. 클론이나 venv는 만들지 않는다(제어기가 만든다).
5. 마지막에 실행 명령을 보여 준다. PyYAML이 있는 python으로 실행해야 한다.
   ```
   tmux new -s research
   python ~/autoresearch/run_research.py --spec ~/autoresearch_runs/<프로젝트명>/<run_id>/00_spec.md
   ```

규칙: 한 번에 너무 많이 묻지 않는다. 사람이 정한 것은 다시 묻지 않는다. 추측한 값은 추측이라고 밝힌다.
