-- 한국 도심 보정 자전거 프로필 — 이미지 내장 bicycle.lua(v5.25.0, BSD-2)를 감싸 계수만 교정.
-- FEAT-007/ADR-009. car.lua·foot.lua 와 동일한 wrapper 방식.
-- 기본 프로필의 주행속도(15km/h)는 국내 도심 자전거 실측 평균(13~16km/h)과 맞아 그대로 두고,
-- 비현실적인 지연 계수만 교정한다:
--   ① 신호등 2s→25s (자전거는 차량 신호를 따르되 우회전 대기가 없어 차량 40s 보다 짧게 잡음)
--   ② 회전 6→9s, 유턴 20→25s (교차로 감속·보행자 혼재)
-- 계단(steps)은 내장 프로필이 이미 밀기(push) 속도로 처리하므로 건드리지 않는다.
-- 계수 수정 시 그래프 재생성(osrm-extract) + osrm-bike 재시작 필수.

local bicycle = require('bicycle')   -- 이미지 내장 /opt/bicycle.lua

function setup()
  local profile = bicycle.setup()
  profile.properties.traffic_light_penalty = 25
  profile.properties.u_turn_penalty        = 25
  profile.turn_penalty                     = 9
  return profile
end

return {
  setup = setup,
  process_way = bicycle.process_way,
  process_node = bicycle.process_node,
  process_turn = bicycle.process_turn,
}
