사람이 평가 코드를 이렇게 고치라고 지시했다:
{{instruction}}

{{eval_dir}} 안의 prepare.py, metrics.py, test_gate.py(트랙 B면 run_bench.sh 포함)를 이 지시대로 고친다. metrics.py는 50줄 이내로 유지한다. 기준 정답은 계속 `os.environ["AR_HIDDEN_DIR"]`에만 쓰고 읽는다(지금 환경의 값은 자기 점검용 임시 폴더다). 고친 뒤 prepare.py와 test_gate.py를 실행해 통과하는지 확인한다. 출력 마지막에 무엇을 바꿨는지 세 줄 이내로 적는다.
