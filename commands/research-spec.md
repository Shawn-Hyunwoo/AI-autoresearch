---
description: 자율 연구 실행의 명세서(00_spec.md)를 사람과 문답하며 확정한다 (Phase 0)
argument-hint: <연구 질문>
---

너는 자율 연구 도구(`~/autoresearch/`)의 Phase 0을 진행한다. 목표는 사람과 짧게 문답해 연구 명세서 `00_spec.md` 하나를 확정하는 것이다. 실행은 시작하지 않는다.

연구 질문: $ARGUMENTS

먼저 읽을 것:
- `~/autoresearch/templates/00_spec_template.md` (명세서 형식과 기본 모델 배정·호출 상한)
- `~/autoresearch/providers.yaml` (쓸 수 있는 벤더, 모델 별명, 추론 단계. 명세에 옮겨 적지 않는다)
- 기본값: `<프로젝트>/.autoresearch/defaults.yaml`이 있으면 그 값으로 템플릿을 채운다. 없으면 `~/autoresearch_runs/<프로젝트명>/*/00_spec.md` 중 가장 최근 것의 값을 쓴다. 둘 다 없으면 템플릿 값 그대로다.
- `~/autoresearch/prompts/_writing_rules.md` (질문과 설명에 적용할 서술 규칙)
- 지금 폴더가 git 저장소면 그 저장소의 CLAUDE.md 또는 AGENTS.md

원칙:
- 문답은 많아야 두 번이다. 첫째는 빠진 필수 항목과 어긋나 보이는 값을 한꺼번에 묻는 것, 둘째는 요약을 보여 주고 "이대로 쓸까요?"를 묻는 것이다. 빠진 것도 어긋난 것도 없으면 첫째를 건너뛴다.
- 사용자가 준 값은 그대로 쓴다. 다시 정리해서 확인받지 않는다.
- 구현 세부는 묻지 않고 정하지도 않는다. 데이터 분할 비율, 결과·예측 파일 형식, 시드 쓰는 법, 비교 기준을 맞추는 방법(파라미터 수 등), 입력 변형(regime)의 이름과 값이 여기에 든다. 이런 것은 파일럿 단계의 builder가 정하고, 사람은 파일럿 관문에서 채점 코드로 확인한다. 명세 2절에는 "구현 세부는 builder가 정한다"고 적고, 사용자가 준 제약(쓸 수 있는 패키지, 실행 시간, 데이터 규모 등)만 옮긴다.

순서:
1. 연구 질문을 한두 문장으로 다시 말하고 트랙(A 효용 실증, B 코드 최적화, C 정리 증명)을 정한다. 사용자가 트랙을 줬으면 그것을 쓴다.
2. 필수 항목 가운데 사용자가 주지 않은 것만 모은다. 저장소에서 알 수 있는 것(source_repo, base_branch, git-ignored 입력 후보)은 직접 확인해 채운다.
   - source_repo, base_branch, base_env, setup_cmds, build_cmd, copy_inputs, push_remote
   - evaluation_contract: guard(깨지면 안 되는 조건), metric(지표와 방향), 트랙 B면 min_improvement_pct
   - budget: 쓰는 트랙에 필요한 항목만 적는다. 공통은 max_crash_repairs, run_limits. 트랙 A·B는 max_iterations, 트랙 A는 seeds, 트랙 B는 bench_rounds, 트랙 C는 max_debate_rounds. call_timeout_min은 선택이다.
   - stages: 쓰는 트랙의 단계를 빠짐없이 적는다. 사용자가 주지 않은 단계는 기본값을 쓴다. 모델과 추론 단계는 providers.yaml에 있는 것만 쓴다.
3. 사용자가 준 값을 연구 주제와 견주어 본다. 어긋나 보이는 값만 이유 한 줄과 함께 확인받는다. 이상한 점이 없으면 이 단계를 건너뛴다. 확인할 만한 예:
   - 모델 배정이 일의 무게와 맞지 않는다. 조사는 강한 모델인데 구현, 수리, 본 실험, 반박, 감사는 약한 모델이거나 낮은 추론 단계인 경우. 트랙 C의 증명이나 반박을 낮은 추론 단계로 두는 경우.
   - 트랙이 질문과 맞지 않는다.
   - 시도 횟수, 시드 수, 라운드 수, 호출 상한이 주제에 비해 너무 작거나 크다.
   - guard나 metric이 질문에 답하지 못하거나 계산할 수 없다.
   - 제어기가 거부할 조합이다. 트랙 C의 prover와 refuter가 같은 벤더이거나, Phase 4 auditors에 Phase 3 주력 모델의 벤더가 들어 있는 경우.
4. 명세 본문에 연구 질문, 가설, 허용 가정을 평문으로 적는다.
5. 10줄 이내로 요약해 보여 주고 "이대로 쓸까요?"를 묻는다. 요약에는 트랙, 기준 env, guard와 metric, budget, 모델 배정 한 줄 요약, 사용자가 주지 않아 기본값으로 채운 항목을 넣는다. 그 밖의 설명은 붙이지 않는다.
6. 확정되면 실행 폴더 `~/autoresearch_runs/<프로젝트명>/<YYYY-MM-DD>_<짧은_영문_주제>/`를 만들고 두 파일을 둔다. 프로젝트명은 source_repo 폴더 이름이다. 클론이나 venv는 만들지 않는다(제어기가 만든다).
   - `00_spec.md`: 확정한 명세
   - `providers.yaml`: `~/autoresearch/providers.yaml`의 사본 (이 실행은 이 사본을 쓴다)
7. 프로젝트에서 바로 볼 수 있게 심볼릭 링크를 만든다. 실제 파일은 실행 폴더에 하나뿐이고, 링크로 열어 고쳐도 같은 파일이 고쳐진다.
   ```bash
   mkdir -p <프로젝트>/.autoresearch/runs/<run_id>
   ln -s ~/autoresearch_runs/<프로젝트명>/<run_id>/00_spec.md <프로젝트>/.autoresearch/runs/<run_id>/00_spec.md
   ln -s ~/autoresearch_runs/<프로젝트명>/<run_id>/providers.yaml <프로젝트>/.autoresearch/runs/<run_id>/providers.yaml
   grep -qxF '/.autoresearch/runs/' <프로젝트>/.git/info/exclude || echo '/.autoresearch/runs/' >> <프로젝트>/.git/info/exclude
   ```
   `.git/info/exclude`는 커밋되지 않는 로컬 설정이라 원본 브랜치는 바뀌지 않는다.
8. 마지막에 이렇게 안내한다.
   - 기본값 그대로 갈 거면 바로 실행한다.
   - 모델 배정이나 호출 상한을 바꾸려면 `.autoresearch/runs/<run_id>/00_spec.md`의 `stages`, `budget`을 고친다.
   - 모델 ID나 CLI 플래그를 바꾸려면 같은 폴더의 `providers.yaml`을 고친다(이 실행에만 적용).
   - 고친 뒤 tmux 안에서 한 번만 실행한다. PyYAML이 있는 python이어야 한다. 같은 실행을 다른 곳에서 또 띄우면 제어기가 거부한다.
   ```bash
   tmux new -s research
   python ~/autoresearch/run_research.py --spec .autoresearch/runs/<run_id>/00_spec.md   # 프로젝트 폴더에서
   ```

규칙: 사람이 정한 것은 다시 묻지 않는다(3번의 어긋난 값만 예외). 추측한 값은 추측이라고 밝힌다. 묻고 설명하는 글은 짧게 쓴다.
