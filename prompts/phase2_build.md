너는 평가 하네스와 파일럿을 만든다. 트랙: {{track}}

연구 명세 (evaluation_contract의 guard와 metric을 따른다):
{{spec}}

조사 결과:
{{recon}}

선별 결과:
{{selected}}

만들 것:
1. {{eval_dir}}/prepare.py: 고정된 입력, 데이터 분할, 테스트 인스턴스를 만들어 {{eval_dir}} 아래에 저장한다. 기준 정답(레이블, 기대 출력 등 채점에만 쓰는 값)은 `os.environ["AR_HIDDEN_DIR"]` 폴더에만 저장한다. 정답을 {{eval_dir}}나 저장소 안에 두지 않는다. 시드를 고정한다. 저장소에 검증된 로더가 있으면 그것을 감싸 쓴다.
2. {{eval_dir}}/metrics.py: 50줄 이내. `python metrics.py <result_dir>`로 실행된다. result_dir의 결과 파일과 prepare.py가 만든 기준만 읽어 guard 통과 여부와 metric을 계산하고, stdout 마지막 줄에 JSON 한 줄을 출력한다:
   {"guard_pass": true, "guard_reason": "...", "primary_metric": 0.0, "direction": "higher_is_better|lower_is_better", "wall_time_sec": null, "secondary_metrics": {}}
   기준 정답은 `os.environ["AR_HIDDEN_DIR"]`에서 읽는다. 모델 초기화, 학습, 알고리즘 실행은 하지 않는다. 주석은 각 지표의 수식 정의와 입출력 의미만 짧게 단다.
3. {{eval_dir}}/test_gate.py: 기준 정답은 `AR_HIDDEN_DIR`에서 읽는다. 세 가지를 검사하고 모두 통과하면 exit 0, 하나라도 실패하면 exit 1. (a) 작은 정답 결과를 통과시킨다 (b) 일부러 만든 불량 결과를 기각한다 (c) SoTA 기준선 하나를 재현한다.
4. 트랙 B가 포함되면 {{eval_dir}}/run_bench.sh: `bash run_bench.sh <result_dir>`로 실행되어, 작은 규모의 정확도(guard) 세트와 중대규모의 속도·메모리(metric) 세트를 차례로 돌려 result_dir에 결과를 쓴다. 이 파일은 잠기고, 이후에는 소스 코드만 바뀐다.
5. 트랙 A가 포함되면 선별된 후보와 SoTA 기준선마다 {{pilot_dir}}/<id>/run.sh: `bash run.sh <result_dir>`로 실행되어 가장 작은 규모로 돌고 result_dir에 metrics.py가 읽을 결과를 쓴다. 난수는 환경변수 `AR_SEED`(정수)를 시드로 쓴다. run.sh와 실험 코드는 AR_HIDDEN_DIR을 읽지 않는다. 실험 코드는 저장소 안에 새 모듈로 두는 것을 우선한다.

만든 뒤 각 스크립트를 한 번씩 직접 실행해 오류가 없는지 확인한다. 지금 환경의 AR_HIDDEN_DIR은 자기 점검용 임시 폴더이고 호출이 끝나면 지워진다. 실제 정답은 제어기가 prepare.py를 다시 실행해 만든다. 출력 마지막에 만든 파일과 각 파일이 하는 일을 한 줄씩 적는다.
