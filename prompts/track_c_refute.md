트랙 C, 라운드 {{n}}. 너는 반박자다. 아래 증명을 공격한다.

연구 명세:
{{spec}}

선행연구 조사 (Known Results 포함):
{{known}}

증명:
{{proof}}

할 일:
1. 논리 허점을 찾는다.
2. 가정이 명세의 질문이 다루는 경우(대상 회로군, 함수 종류, 크기 범위 등)를 덮는지 판정한다(scope_realistic). 덮지 못하면 빠진 경우를 적는다. 증명이 알려진 정리 하나를 대입하는 것으로 끝나는지, 기존 정리의 표기만 바꾼 것인지, 선행연구 조사의 Known Results에 이미 있는 결과인지도 판정한다(non_trivial). 하나라도 해당하면 false다.
3. {{round_dir}}/counterexample_hunter.py를 작성한다. 공간 전체에 무작위 점을 뿌리지 않고, 정리가 깨질 만한 곳을 골라 넣는다. 아래 네 범주를 모두 시험하고, 고른 점마다 상대 크기 1e-3과 1e-1로 흔든 입력을 3개씩 함께 넣는다.
   - extreme(양극단): 가정이 허용하는 경계값, 아주 작거나 큰 크기와 파라미터
   - singular(특이점): 0, 퇴화(계수 부족 행렬, 겹치는 고윳값 등), 분모가 0에 가까운 곳
   - equality(등호 근처): 양변이 같아지는 입력과 그 바로 근처. 강화한 정리의 반례는 대개 여기 있다. 등호가 성립하는 입력이 없으면 그 이유를 no_equality_reason에 적는다
   - typical(평범한 값): 가정 안의 전형적인 입력
   정리가 상한("오차는 많아야 B(n)" 꼴)이면, 크기 n을 두 배씩 키운 작은 크기 셋 이상에서 실제 값과 B(n)을 잰다. B(n) ÷ 실제 값이 크기에 따라 계속 커지면 tightness_ok를 false로, 일정하거나 줄면 true로 둔다. 상한이 아닌 정리면 true로 둔다. stdout 마지막 줄에 JSON 한 줄을 출력한다(by_category는 범주별로 시험한 입력 수):
   {"tested_cases": 0, "by_category": {"extreme": 0, "singular": 0, "equality": 0, "typical": 0}, "no_equality_reason": null, "counterexamples_found": 0, "worst_counterexample": null, "tightness_ok": true}
   제어기는 범주 하나라도 0이면(등호 근처는 이유를 적은 경우만 예외) 통과시키지 않는다. 지난 라운드에서 빠진 범주: {{prev_missing}}
   본 실행은 제어기가 한다. 너는 범주마다 입력 한두 개만 넣어 돌려 마지막 JSON 줄이 나오는지만 확인한다.

출력 마지막 줄은 JSON 한 줄이다:
{"logic_sound": true, "scope_realistic": true, "non_trivial": true, "fatal_flaws": []}
