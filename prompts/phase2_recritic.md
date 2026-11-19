발산자가 {{round}}회차 답을 냈다(최대 {{max_rounds}}회). 앞 회차에 revise로 둔 후보({{open_ids}})만 다시 판정한다. 기준과 판정 종류는 1회차와 같다. 반론이 타당하면 받아들인다.
{{final_rule}}

발산자의 답:
{{reply}}

출력 끝에 1회차와 같은 형식의 ```json 블록을 하나 둔다. 다시 판정한 후보만 넣는다.
```json
{"verdicts": [{"id": "D1_A", "verdict": "accept|revise|reject", "reason": "한두 줄", "request": "revise일 때만"}]}
```
