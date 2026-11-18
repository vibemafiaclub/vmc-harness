"""회귀 세트 (spec 11) — 모델 교체·릴리스 전에 돌려 기준선보다 같거나 나은지 본다.

데이터셋은 고객 데이터라 레포 밖 `~/vmc-client-data/<고객>/eval/`에 둔다(NDA). 결과도 거기 `results/`에.
평가 때만 모델을 바꾸는 스위치는 환경변수 `VMC_HARNESS_EVAL_MODEL`(core.llm이 읽는다). 운영 코드는 모델 이름을 그대로 둔다.

세트: ① diagnosis(진단 정답지) ② classify(학습 후보 분류) ③ render(렌더링 대조) ④ overhead(오버헤드) ⑤ acceptance(제안 수용률)
⑥ learning(학습 발화 라벨 — 답·선택·승인을 정정으로 읽지 않는가)
"""

SETS = ("diagnosis", "classify", "render", "overhead", "acceptance", "learning")
