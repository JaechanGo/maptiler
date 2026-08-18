# 지오코더(geocode-pg) 검증 전용 컨테이너 배선

작업 워크트리의 `server/geocode-api-pg.py` 를 **그 워크트리에서 곧바로** 확인하기 위한 절차다.
`docker compose` 를 쓰지 않고 `docker run` 으로 독립 컨테이너를 띄운다. (T029 에서 확립)

## 왜 compose 가 아닌가

`docker compose` 는 프로젝트명을 **compose 파일이 있는 디렉터리 이름**에서 유도한다.
이 저장소는 `server/docker-compose.yml` 이므로 어느 워크트리에서 실행하든 프로젝트명이
전부 `server` 로 **충돌**한다. 즉 워크트리에서 `docker compose up` 을 하면 새 스택이 생기는 게
아니라 **기존 운영 스택의 소유권을 인수(adopt)해 그 자리에서 recreate** 한다.
`geocode-pg` 는 `depends_on: postgis` 이므로 1,628만 행이 들어 있는 `server-postgis-1` 까지
재생성 대상이 된다 — 가능성이 아니라 기본 동작이다.

부수적으로 `docker-compose.yml` 이 참조하는 부모상대 바인드(`../tiles`, `../vendor`,
`../geocode`)는 워크트리에 존재하지 않아 Docker 가 조용히 빈 디렉터리로 만들어 버린다.

`docker run` 은 compose 프로젝트라는 개념 자체를 우회하므로 위 충돌이 성립하지 않는다.

## 기동

전제(실측): 이미지 `cuvia-geocode-pg:local` 존재(`CMD ["python3","/app/geocode-api-pg.py"]`,
ENTRYPOINT 없음), 네트워크 `server_cuvia` 에 `server-postgis-1` 이 붙어 있어 호스트명
`postgis` 로 접근 가능.

```bash
docker run -d --name t029-geocode-pg \
  --network server_cuvia \
  -p 127.0.0.1:8093:8082 \
  -e DATABASE_URL='postgresql://cuvia:cuvia@postgis:5432/cuvia' \
  -e GEOCODE_PORT=8082 \
  -v <워크트리>/server/geocode-api-pg.py:/app/geocode-api-pg.py:ro \
  cuvia-geocode-pg:local
```

포트는 검증용으로 8093 을 쓴다(운영 compose 는 8082·8092 를 이미 점유).

## 반영과 함정

- 코드 수정 후 반영: `docker restart t029-geocode-pg` (bind mount 이므로 재빌드 불요)
- **단일 파일 bind mount 는 inode 를 고정한다.** `git checkout` / `git stash` 로 파일이 교체되면
  컨테이너는 삭제된 옛 inode 를 계속 물고 있어 **고쳤는데 응답이 그대로**인 유령 현상이 난다.
  브랜치를 오간 뒤에는 `docker rm -f` 후 위 `docker run` 을 **다시** 실행한다. `restart` 로는 낫지 않는다.
- **매 측정 직전 md5 대조를 의무화한다.** 두 값이 다르면 그 측정은 폐기한다.

```bash
docker exec t029-geocode-pg md5sum /app/geocode-api-pg.py
command md5 -q <워크트리>/server/geocode-api-pg.py
```

- 기존 검증 스크립트를 쓸 때는 `OURS=http://127.0.0.1:8093` 을 **반드시** 앞에 붙인다.
  미지정 시 기본값이 운영 서버다.

## 원복

```bash
docker rm -f t029-geocode-pg
```

compose 파일 무수정, `server-geocode-pg-1`·`server-postgis-1` 무접촉이므로 다른 작업에 영향이 없다.
이미지·네트워크는 공용이고 이 절차가 새로 만든 것이 아니므로 삭제하지 않는다.

## before/after 대조 시 주의

기준선(before)은 **코드를 한 줄도 고치기 전에** 이 컨테이너(:8093)에서 뜬다.
`server-geocode-pg-1`(:8092)은 다른 워크트리의 낡은 판을 물고 있을 수 있으므로 기준선으로 쓰지 않는다.
before 와 after 는 **같은 컨테이너·같은 좌표 집합**으로 측정하고, 좌표 집합은 한 번 만든 뒤 고정한다.
