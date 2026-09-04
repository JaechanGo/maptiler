#!/bin/bash
# geocode 타깃이 running 이 되면 서빙 컨테이너·jenkins 를 내려 메모리를 비우고, 끝나면 다시 올린다(15GB 호스트 전용 운영 보조).
# 마커: /home/maptiler/logs/geocode-mem.marker  · 최대 12시간 감시
# 사용(빌드 호스트, root): nohup bash scripts/geocode_mem_watch.sh >/dev/null 2>&1 &  — 전체 체인 큐잉 직전에 띄운다.
# [실측 2026-09-04] 서빙·jenkins 를 내리면 geocode 1h37m 완주, 안 내리면 dedup_er(≈14.5GB) 가 HDD 스왑 쓰레싱으로 3h+ 또는 정체. 스튜디오는 경고만 하고 차단하지 않는다.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
M=/home/maptiler/logs/geocode-mem.marker
COMPOSE="docker compose -f /home/maptiler/server/docker-compose.yml"
SVC="postgis tileserver demo gateway geocode martin geocode-pg osrm-car osrm-foot osrm-bike"
stopped=0
log(){ echo "$(date +%H:%M:%S) $*" >> "$M"; }
for i in $(seq 1 4320); do
  st=$(timeout 6 curl -sN http://127.0.0.1:18081/api/events 2>/dev/null | head -3 | grep -o '"geocode":[^}]*}')
  if [ "$stopped" = 0 ] && echo "$st" | grep -q running; then
    log "geocode running 감지 → 서빙·jenkins 정지"
    systemctl stop jenkins 2>/dev/null
    COMPOSE_PROFILES=postgis $COMPOSE stop $SVC >/dev/null 2>&1
    stopped=1; log "정지 완료: $(docker ps --format '{{.Names}}' | tr '\n' ' ')"
  elif [ "$stopped" = 1 ] && echo "$st" | grep -q '"done"\|"error"\|"skipped"'; then
    log "geocode 종료($st) → 서빙·jenkins 재기동"
    COMPOSE_PROFILES=postgis $COMPOSE start $SVC >/dev/null 2>&1
    sleep 20; docker restart server-martin-1 >/dev/null 2>&1
    systemctl reset-failed jenkins 2>/dev/null; systemctl start jenkins 2>/dev/null
    log "재기동: $(docker ps --format '{{.Names}}' | wc -l)개 컨테이너, jenkins=$(systemctl is-active jenkins)"
    exit 0
  fi
  sleep 10
done
log "12시간 내 geocode 감지/종료 없음 — 종료"
[ "$stopped" = 1 ] && { COMPOSE_PROFILES=postgis $COMPOSE start $SVC >/dev/null 2>&1; systemctl start jenkins 2>/dev/null; }
