"""빌드 스크립트 공용 모듈 (T028).

scripts/*.py 에서는  sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
scripts/postgis/*.py 에서는  ... os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
를 선행한 뒤 `from _common.xxx import ...` 로 쓴다. PYTHONSAFEPATH=1 환경에서도
스크립트 디렉터리가 sys.path 에 자동 추가되지 않으므로 이 프리앰블은 생략할 수 없다.
"""
