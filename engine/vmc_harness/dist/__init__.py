"""배포·라이선스·원격 서버·업데이트 (spec 10).

- shell.py   CLI 껍데기 원본(혼자 도는 파일 — install.py가 설치, activate·커널 설치·검증 로직이 여기 한 벌)
- kernel.py  커널 설치 경로·current 해석
- server.py  apps/api 호출 call()
- lock.py    프로젝트당 루프 1개 잠금(서버, 못 닿으면 로컬 파일)
- updater.py 세션 시작 자동 업데이트(하루 1회, 백그라운드)
- uninstall.py 사용자 홈 정리 remove_home()
"""
