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
  -- 계단회피 옵션(exclude=steps) — 휠체어·유모차·캐리어. 내장 foot 프로필은 classes 를
  -- 아예 선언하지 않아(핸들러 목록에도 WayHandlers.classes 없음) 여기서 클래스를 신설하고
  -- process_way 에서 직접 부여한다. excludable 은 그래프 빌드 시점에 구워진다.
  profile.classes = Sequence { 'steps' }
  profile.excludable = Sequence { Set { 'steps' } }
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

-- highway=steps 에 'steps' 클래스를 달아 exclude=steps 로 걸러낼 수 있게 한다.
-- 지하보도·육교 계단이 대표 대상. 클래스만 부여하고 속도·통행 가능 여부는 건드리지 않는다
-- (옵션을 안 켜면 지금까지와 동일한 경로가 나와야 하므로).
function process_way(profile, way, result, relations)
  foot.process_way(profile, way, result, relations)
  if way:get_value_by_key('highway') == 'steps' then
    result.forward_classes['steps'] = true
    result.backward_classes['steps'] = true
  end
end

return {
  setup = setup,
  process_way = process_way,
  process_node = foot.process_node,
  process_turn = foot.process_turn,
}
