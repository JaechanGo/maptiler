-- 한국 도심 보정 도보 프로필 — 이미지 내장 foot.lua(v5.25.0, BSD-2)를 감싸 계수만 교정. FEAT-007/ADR-009.
-- 기본 프로필은 전 구간 5km/h 균일·신호등 2초라 실보행 대비 20~25% 낙관(상용 내비는 4km/h대 + 횡단 대기).
-- 교정 3축: ① 보행속도 5→4.5km/h(신호 대기 별도 계상 전제의 순보행 속도)
--          ② 신호 횡단 대기 2→30s(보행 신호 주기 기대 대기)  ③ 계단 3.5km/h(층계 감속)
-- car.lua 와 동일한 wrapper 방식 — 07-gen-route-graph.sh 가 /opt/kr-foot.lua 로 마운트(내장 덮어쓰기 금지).
-- 계수 수정 시 그래프 재생성(osrm-extract) + osrm-foot 재시작 필수.

local foot = require('foot')        -- 이미지 내장 /opt/foot.lua

local KR_WALKING_SPEED = 4.5        -- km/h
local KR_STEPS_SPEED   = 3.5        -- 계단

function setup()
  local profile = foot.setup()
  profile.properties.traffic_light_penalty = 30
  -- speeds 는 그룹(highway/railway/amenity/…) 중첩 테이블 — 기본 보행속도(5)만 일괄 치환.
  -- 5 외 값(예: ferry 등 route_speeds)은 건드리지 않는다.
  for _, group in pairs(profile.speeds) do
    for k, v in pairs(group) do
      if v == 5 then group[k] = KR_WALKING_SPEED end
    end
  end
  profile.speeds.highway.steps = KR_STEPS_SPEED
  profile.default_speed = KR_WALKING_SPEED
  return profile
end

return {
  setup = setup,
  process_way = foot.process_way,
  process_node = foot.process_node,
  process_turn = foot.process_turn,
}
