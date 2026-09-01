-- 한국 도심 보정 차량 프로필 — 이미지 내장 car.lua(v5.25.0, BSD-2)를 감싸 계수만 교정. FEAT-007/ADR-009.
-- 근거: 폐쇄망은 실시간 교통을 못 쓴다 → 상용 내비의 "평상시" 추정처럼 정적 비용모델을 현실화한다.
--   기본 프로필은 자유주행 가정(신호 2초·secondary 55km/h·maxspeed 그대로)이라 도심에서 실측 대비
--   약 2배 낙관(실측: 부천 춘의역→상동역 경유 4.8km 를 8.5분으로 계산, 네이버 17분).
-- 교정 3축:
--   1) 신호등 지연 2s→30s (국내 신호주기 120~160s·녹색비 0.35~0.45 → 기대 대기 25~40s. OsmAnd 25s 와 동급)
--   2) 회전 지연 — turn 7.5→15s·유턴 20→45s(신호연동)·turn_bias ↑(보호좌회전 반영)
--   3) 주행속도 = 등급별 도심 실효속도로 하향, OSM maxspeed 태그는 "상한"으로만 사용(기본은 목표속도로 씀)
-- 산출물 호환: osrm-extract 시점에 그래프로 구워짐 — 그래프 재생성(07-gen-route-graph.sh) 필수.
-- 사용법: 07-gen-route-graph.sh 가 이 파일을 /opt/kr-car.lua 로 마운트해 -p 지정(내장 car.lua 를 require 하므로
--         /opt/car.lua 를 덮어쓰면 안 됨 — 자기 자신을 require 하는 무한루프).

local car = require('car')          -- 이미지 내장 /opt/car.lua (require 경로는 프로필 디렉토리 기준)

-- 등급별 도심 실효 주행속도(km/h). "움직일 때" 속도 — 신호 대기는 traffic_light_penalty 로 별도 계상.
-- motorway 는 무신호 자유주행이라 기본값 유지. trunk 는 국내에선 도시고속·자동차전용이 많아 소폭 하향.
local KR_SPEEDS = {
  motorway        = 90,
  motorway_link   = 40,
  trunk           = 65,
  trunk_link      = 35,
  primary         = 40,
  primary_link    = 25,
  secondary       = 32,
  secondary_link  = 22,
  tertiary        = 25,
  tertiary_link   = 18,
  unclassified    = 19,
  residential     = 18,
  living_street   = 10,
  service         = 10,
}

function setup()
  local profile = car.setup()
  profile.properties.traffic_light_penalty = 40   -- 신호등 1개당 기대 지연(s) — OSM 신호 태깅 누락 보상 포함(실측 보정)
  profile.properties.u_turn_penalty        = 45   -- 국내 유턴은 대부분 신호 연동
  profile.turn_penalty                     = 15   -- 교차로 회전 지연 상한(시그모이드 최대치, s)
  profile.turn_bias                        = 1.15 -- 좌회전(진행방향 역측) 가중 — 보호좌회전 대기 반영
  for k, v in pairs(KR_SPEEDS) do
    profile.speeds.highway[k] = v
  end
  return profile
end

-- 기본 WayHandlers.maxspeed 는 maxspeed 태그를 목표속도로 승격(×0.8)해 위 속도표를 무효화한다
-- (예: 길주로 maxspeed=60 → 48km/h 로 역상승). 등급 실효속도를 "상한"으로 재적용해 캡만 남긴다.
-- min 이므로 스쿨존(30) 등 태그가 더 낮으면 태그 쪽이 그대로 이긴다. highway 미해당(페리 등)은 불개입.
function process_way(profile, way, result, relations)
  car.process_way(profile, way, result, relations)
  local class_speed = KR_SPEEDS[way:get_value_by_key('highway')]
  if class_speed then
    if result.forward_speed > class_speed then result.forward_speed = class_speed end
    if result.backward_speed > class_speed then result.backward_speed = class_speed end
  end
end

return {
  setup = setup,
  process_way = process_way,
  process_node = car.process_node,
  process_turn = car.process_turn,
}
