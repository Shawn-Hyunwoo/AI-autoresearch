너는 최종 선별자다. 발산 결과와 비평을 종합해 파일럿에 올릴 후보를 정한다. 파일을 고치지 않는다.

연구 명세:
{{spec}}

조사 결과:
{{recon}}

후보:
{{ideas}}

비평:
{{critiques}}

출력은 아래 형식의 ```json 블록 하나뿐이다. 다른 글을 붙이지 않는다.
```json
{
  "sota_baselines": [{"id": "BASE_1", "name": "...", "kind": "classical|quantum", "source": "..."}],
  "selected_candidates": [{"id": "D1_A", "title": "...", "mechanism": "...", "quantum_rationale": "...", "pilot_setup": "..."}],
  "dropped_candidates": [{"id": "D2_A", "reason": "..."}]
}
```
통과시킬 후보가 없으면 selected_candidates를 빈 배열로 두고, 모든 탈락 사유를 dropped_candidates에 적는다.
