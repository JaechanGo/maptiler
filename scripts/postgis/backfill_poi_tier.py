#!/usr/bin/env python3
"""T8: poi.tier_minzoom 백필 — theme.json의 poi_tiers+cat_tiers(단일소스)에서 각 POI의
표시 시작 줌을 계산해 tier_minzoom 에 UPDATE. 멱등(전 행 갱신). 행 삭제 없음.

DB접속: load_geocode.py 방식과 동일(libpq 환경변수 / 기본 cuvia/cuvia@localhost:5433).
"""
import json, os, subprocess, sys, time

# style_objects 기본값 참조 (scripts/ 아래)
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..")
sys.path.insert(0, SCRIPTS)
import style_objects  # noqa: E402

# ── theme.json 로드 (있으면 우선) ──────────────────────────────────────────
THEME_PATH = os.path.join(SCRIPTS, "..", "style", "theme.json")
theme = {}
if os.path.exists(THEME_PATH):
    try:
        theme = json.loads(open(THEME_PATH, encoding="utf-8").read())
    except Exception as e:
        print(f"[warn] theme.json 로드 실패({e}), style_objects 기본값 사용", file=sys.stderr)

# ── poi_tiers: tier key → minzoom ─────────────────────────────────────────
def _parse_poi_tiers(raw):
    """[{"key":"t1","minzoom":15}, ...] 또는 {"t1":15, ...} → {key: minzoom}."""
    if isinstance(raw, list):
        return {item["key"]: int(item["minzoom"]) for item in raw if "key" in item and "minzoom" in item}
    if isinstance(raw, dict):
        return {k: int(v) for k, v in raw.items()}
    return {}

raw_tiers = theme.get("poi_tiers") or style_objects.POI_TIERS_DEFAULT
tier_minzoom_map = _parse_poi_tiers(raw_tiers)

# 기본값 폴백(스타일 객체)
if not tier_minzoom_map:
    tier_minzoom_map = {item["key"]: item["minzoom"] for item in style_objects.POI_TIERS_DEFAULT}

fallback_tier = theme.get("poi_tier_fallback") or style_objects.POI_TIER_FALLBACK
fallback_minzoom = tier_minzoom_map.get(fallback_tier, 17)

# ── cat_tiers: cat1/cat2 → minzoom ────────────────────────────────────────
def _parse_cat_tiers(raw, tier_map):
    """{"cat1": {cat: tier, ...}, "cat2": {...}} → cat1_map, cat2_map (값=minzoom)."""
    c1, c2 = {}, {}
    if not isinstance(raw, dict):
        return c1, c2
    for cat, tier in (raw.get("cat1") or {}).items():
        c1[cat] = tier_map.get(tier, fallback_minzoom)
    for cat, tier in (raw.get("cat2") or {}).items():
        c2[cat] = tier_map.get(tier, fallback_minzoom)
    return c1, c2

raw_cat = theme.get("cat_tiers") or style_objects.CAT_TIER_DEFAULT
cat1_map, cat2_map = _parse_cat_tiers(raw_cat, tier_minzoom_map)

print(f"[tier] tier_minzoom_map={tier_minzoom_map}", file=sys.stderr)
print(f"[tier] fallback={fallback_tier}→{fallback_minzoom}", file=sys.stderr)
print(f"[tier] cat1_map={cat1_map}", file=sys.stderr)
print(f"[tier] cat2_map={cat2_map}", file=sys.stderr)

# ── DB 접속 (load_geocode.py 방식 동일: libpq 환경변수) ──────────────────
env = dict(os.environ)
env.setdefault("PGHOST", "localhost")
env.setdefault("PGPORT", "5433")
env.setdefault("PGUSER", "cuvia")
env.setdefault("PGDATABASE", "cuvia")
env.setdefault("PGPASSWORD", "cuvia")

# ── CASE SQL 생성 ─────────────────────────────────────────────────────────
# cat2 우선 → cat1 → fallback.
# psql -c 로 실행(load_geocode.py 와 동일하게 subprocess+psql).

def _sql_case_when(mapping, col):
    """매핑 dict → SQL CASE WHEN 조각."""
    if not mapping:
        return None
    lines = []
    for cat, mz in mapping.items():
        safe = cat.replace("'", "''")
        lines.append(f"    WHEN {col} = '{safe}' THEN {mz}")
    return "CASE\n" + "\n".join(lines) + "\n  END"

c2_case = _sql_case_when(cat2_map, "cat2")
c1_case = _sql_case_when(cat1_map, "cat1")

# COALESCE(cat2 CASE, cat1 CASE, fallback)
coalesce_parts = []
if c2_case:
    coalesce_parts.append(c2_case)
if c1_case:
    coalesce_parts.append(c1_case)
coalesce_parts.append(str(fallback_minzoom))

tier_expr = "COALESCE(\n  " + ",\n  ".join(coalesce_parts) + "\n)"

update_sql = f"""
UPDATE poi SET tier_minzoom = {tier_expr};
SELECT tier_minzoom, count(*) FROM poi GROUP BY 1 ORDER BY 1;
"""

print("[backfill] tier_minzoom UPDATE 시작 …", file=sys.stderr)
t0 = time.time()
r = subprocess.run(["psql", "-v", "ON_ERROR_STOP=1"], input=update_sql, text=True, env=env)
if r.returncode != 0:
    sys.exit("✗ psql backfill_poi_tier 실패")
print(f"[backfill] 완료 · {time.time()-t0:.0f}s", file=sys.stderr)
