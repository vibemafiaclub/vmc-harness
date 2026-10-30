"""dist 설정 읽기. 배포·업데이트·서버 설정은 PC 단위라 프로젝트 프로파일과 무관하다 —
프로젝트 루트를 찾지 않는다(작업 폴더가 프로젝트 밖이거나 지워졌어도 업데이트가 돈다)."""

from vmc_harness.core import config


def get(key):
    return config.get(key, profile="standard")
