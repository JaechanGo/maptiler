#!/usr/bin/env python3
"""T046 §4.3 — 존재 오라클·본번 근사·심판 조회.

"우리 DB 에 이 주소가 **있기는 한가**"를 판정한다. 이것이 없으면 "지오코딩이
틀렸다"와 "애초에 자료가 없다"를 구분할 수 없고, 후자를 전자로 세면 정확도가
실제보다 낮게 나온다.

## 파티션 pruning — 263 배가 걸린 지점(§1.12)

`parcel` 은 `LIST (sido_cd)` 파티션 테이블이다. 조회 조건에서 파티션 키를
**표현식으로 가리면**(`substr(pnu,1,2)` 같이) 플래너가 어느 파티션을 볼지
결정하지 못해 18 개 파티션을 전부 훑는다. 실측 1,000 키 기준:

    substr(pnu,1,8) 로 emd_cd 유도 …… 69,430 ms
    사전 파싱한 컬럼을 그대로 바인딩 …… 263.9 ms   (263 배)

그래서 PNU 를 **파이썬에서** 쪼개(`split_pnu`) `sido_cd`·`emd_cd`·`san`·
`ji_main`·`ji_sub` 를 각각 넘긴다. SQL 안에서는 절대 자르지 않는다.
`test_oracle.py::TestSqlShape` 가 이 규약을 문자열로 감시한다.

`address` 는 파티션이 아니다. 대신 전용 색인
`((bcode || substr(bd_mgt_sn, 11, 9))) WHERE kind = 'addr'` 가 있으므로
**정확히 같은 표현식**을 써야 색인을 탄다. 여기서의 `substr` 은 파티션 키가
아니라 색인 정의의 일부라 위 금지와 무관하다.

## 조건 1(Critical) — 시도코드 12 완화

원천 202607 은 광주·전남을 시도코드 **12** 로 합쳤는데 우리 DB 는 아직
광주 `29` / 전남 `46` 이다. 접두가 12 인 PNU 는 엄격 조회로 **전건 미적중**이
되므로, 미적중 건에 한해 `46` → `29` 순으로 재조회한다. 나머지 17 자리는
그대로 둔다.

완화는 **미적중 건에만** 건다. 처음부터 완화된 후보까지 조회하면 완화 없이
맞힌 건수와 완화로 건진 건수를 구분할 수 없다. `relax12_hits`(완화로 건진 수)와
`relax12_attempts`(완화를 시도한 수)를 F6 와 **별개로** 집계한다 — F6 은
정규화 비교축의 플래그이고 이쪽은 오라클 조회축의 계수기다.

## 키 취급

배치 SQL 의 `i` 열에는 호출자가 준 키를 **그대로** 싣는다. 순번을 새로 매기면
부분집합 재조회(예: address 미적중분만 parcel 로) 때 순번이 밀려 결과가
엉뚱한 키에 붙는다.
"""
import pgprobe
from normalize import SIDO12, SIDO12_PARENTS

__all__ = [
    "Oracle", "build_pnu", "pnu_from_bm25", "resolve_pnu",
    "split_pnu", "sido_relax_candidates", "sido_legacy_candidates", "SIDO_LEGACY",
    "sql_addr_batch", "sql_parcel_batch", "sql_apx_batch",
    "sql_referee_parcel_batch", "sql_road_bm25_batch",
]

PNU_LEN = 19
BM25_LEN = 25
BCODE_LEN = 10


# ── PNU 조립·분해 ─────────────────────────────────────────────────────
def build_pnu(bcode, san, ji_main, ji_sub):
    """§4.3-a — 법정동코드(10) + 대지구분(1) + 본번(4) + 부번(4).

    대지구분은 원천 `[05]` 산 여부를 `0 → '1'`(일반), `1 → '2'`(산) 로 옮긴 값이다.
    뒤집으면 산번지 전 건이 조용히 어긋난다.
    """
    b = "" if bcode is None else str(bcode).strip()
    if len(b) != BCODE_LEN or not b.isdigit():
        raise ValueError("법정동코드가 10 자리 숫자가 아니다: %r" % (bcode,))
    m, s = int(ji_main), int(ji_sub)
    if not (0 <= m <= 9999) or not (0 <= s <= 9999):
        raise ValueError("본번/부번이 4 자리를 넘는다: %r-%r" % (ji_main, ji_sub))
    return "%s%s%04d%04d" % (b, "2" if san else "1", m, s)


def pnu_from_bm25(bm25):
    """건물관리번호 25 자리의 앞 19 자리가 곧 PNU 다."""
    v = "" if bm25 is None else str(bm25).strip()
    if len(v) != BM25_LEN:
        raise ValueError("건물관리번호가 25 자리가 아니다: %r" % (bm25,))
    return v[:PNU_LEN]


def resolve_pnu(bcode, san, ji_main, ji_sub, bm25):
    """조건 5(Minor) — 이중 경로가 갈리면 `BM25[:19]` 를 채택하고 계수한다.

    `(pnu, mismatch)` 를 돌려준다. 건물관리번호가 없으면 조립본을 쓰고
    `mismatch` 는 False 다(비교 대상이 없는 것은 불일치가 아니다).
    """
    assembled = build_pnu(bcode, san, ji_main, ji_sub)
    if not bm25:
        return assembled, False
    from_bm25 = pnu_from_bm25(bm25)
    return from_bm25, from_bm25 != assembled


def split_pnu(pnu):
    """PNU → 조회 컬럼. **SQL 이 아니라 여기서** 자른다(파티션 pruning).

    `emd_cd` 는 **8 자리**다 — `parcel.emd_cd` 가 char(8) 이라 그 컬럼과 맞추려면
    그래야 한다. 그런데 PNU 의 법정동코드는 **10 자리**(시도2+시군구3+읍면동3+
    **리2**)이므로 이 키만으로 조인하면 **리가 사라진다.** 그래서 `bcode`(10)와
    원본 `pnu`(19)를 함께 내놓는다. 8 자리는 색인용, 10·19 자리는 식별용이다.
    """
    p = str(pnu)
    if len(p) != PNU_LEN:
        raise ValueError("PNU 가 19 자리가 아니다: %r" % (pnu,))
    return {
        "pnu": p,
        "sido_cd": p[:2],
        "emd_cd": p[:8],              # 색인 `parcel_jibun_lookup` 의 선두 컬럼
        "bcode": p[:10],              # 리 2 자리를 포함한 법정동코드
        "san": int(p[10]) - 1,        # '1'→0, '2'→1 (parcel.san 은 smallint 0/1)
        "ji_main": int(p[11:15]),
        "ji_sub": int(p[15:19]),
    }


# 설계 결정 — 완화·보정을 **"PNU 조립 직후"가 아니라 "미적중 건 사후 재조회"**로
# 건다. 계획 조건 1 원문은 전자를 지시했다. 후자를 택한 근거는 세 가지다.
#
#   1. 계수 분리. 사후 재조회는 "엄격 조회로는 못 찾았는데 완화로 찾았다"를
#      키 단위로 남긴다(`relax12_keys`·`legacy_keys`). 조립 시점에 후보를 늘리면
#      어느 건이 완화의 산물인지 사후에 복원할 수 없고, §8-10 이 요구한
#      엄격/완화 병기가 불가능해진다.
#   2. 엄격 수치의 보존. 사전 확장은 엄격 분모 자체를 바꾼다. 그러면 "완화가
#      없었다면" 수치를 만들 수 없다.
#   3. 비용. 발동 건은 표본의 13% 뿐이라 재조회 배치가 훨씬 작다.
#
# 기능적으로는 동등하다 — 두 방식이 같은 적중 집합을 낸다(`legacy=False` 대조군
# 테스트가 이를 고정한다). 순서 의존이 없으므로 결과는 경로와 무관하다.
def sido_relax_candidates(pnu):
    """조건 1 — 접두 12 를 46 → 29 순으로 확장한다. 나머지 17 자리는 보존."""
    p = str(pnu)
    if len(p) != PNU_LEN or p[:2] != SIDO12:
        return []
    return [parent + p[2:] for parent in SIDO12_PARENTS]


# 조건 1-b — 원천 구 시도코드 대 DB 현행 시도코드의 어긋남.
#
# 실측: 원천 202607 은 강원을 `42`, 전북을 `45`(구 코드)로 싣는데 우리 `parcel`
# 은 `51`·`52`(현행)로 적재돼 있다. `pg_inherits` 확인 결과 `parcel_42`·
# `parcel_45` 파티션은 **존재하지 않고** `parcel_51`(2,757,489 행)·
# `parcel_52`(3,887,040 행)만 있다. 보정하지 않으면 해당 건이 전부 `O=N` 이 되어
# "우리 DB 에 없다"로 오판되고, 분류 3·4(= 우리 결함 아님, 회수율 0)로 흘러
# **우리 결함이 면책된다.** 계획 C3 가 막으려 한 자기유리 편향과 같은 계열이다.
#
# 조건 1(시도코드 12)과 **방향이 반대**라는 점에 주의. 전남광주는 원천도 DB 도
# 구 코드(46/29)라 일관되어 relax12 가 발동하지 않는 것이 정상이다. 강원·전북만
# 빌드 파이프라인이 재매핑했다. 그래서 relax12 와 **별도 계수기**로 집계한다.
SIDO_LEGACY = {"42": ("51",), "45": ("52",)}


def sido_legacy_candidates(pnu):
    """구 시도코드 → 현행 코드. 나머지 17 자리는 보존한다."""
    p = str(pnu)
    if len(p) != PNU_LEN:
        return []
    return [new + p[2:] for new in SIDO_LEGACY.get(p[:2], ())]


# ── SQL 생성 ──────────────────────────────────────────────────────────
def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def _values(n):
    """n 행짜리 자리표시자. 실제 리터럴은 `Oracle` 이 채운다."""
    return ",".join(["%s"] * max(int(n), 0))


def sql_parcel_batch(n):
    """지적 필지 존재(O=P). **정확 PNU 19 자리**로 대조한다.

    F-ri — 이전 판은 `(sido_cd, emd_cd8, san, ji_main, ji_sub)` 로 조인했다.
    `emd_cd` 가 8 자리라 **리 2 자리가 빠져** 같은 읍면동의 다른 리에 있는
    동일번지 필지를 존재 근거로 셌다. `parcel_sido_cd_pnu_key (sido_cd, pnu)` 는
    UNIQUE 이므로 이 조인이 **더 옳으면서 더 싸다.** `sido_cd` 리터럴이 앞에
    있어 파티션 pruning 도 그대로다.
    """
    return (
        "/* t046:parcel */\n"
        "WITH k(i, sido_cd, pnu) AS (VALUES %s)\n"
        "SELECT DISTINCT k.i FROM k WHERE EXISTS (\n"
        "  SELECT 1 FROM parcel p\n"
        "  WHERE p.sido_cd = k.sido_cd AND p.pnu = k.pnu)"
        % _values(n)
    )


def sql_apx_batch(n):
    """본번 근사(`O_apx`) — 부번을 보지 않는다. '본번은 있는데 부번이 없다'를 가른다.

    F-ri — 무시하는 것은 **부번뿐**이어야 한다. 리까지 무시하면 이웃 리의 같은
    본번을 근사 적중으로 세게 된다. 정확 PNU 로는 조인할 수 없으므로(부번을
    일부러 버린다) 색인 `parcel_jibun_lookup (emd_cd, ji_main, ji_sub)` 경로는
    유지한 채 `substr(p.pnu, 1, 10)` 로 **리를 되건다.**
    """
    return (
        "/* t046:apx */\n"
        "WITH k(i, sido_cd, emd_cd, bcode, san, ji_main) AS (VALUES %s)\n"
        "SELECT DISTINCT k.i FROM k WHERE EXISTS (\n"
        "  SELECT 1 FROM parcel p\n"
        "  WHERE p.sido_cd = k.sido_cd AND p.emd_cd = k.emd_cd\n"
        "    AND p.san = k.san AND p.ji_main = k.ji_main\n"
        "    AND substr(p.pnu, 1, 10) = k.bcode)"
        % _values(n)
    )


def sql_addr_batch(n):
    """주소 존재(O=A). 전용 색인의 표현식을 **글자 그대로** 재현한다."""
    return (
        "/* t046:addr */\n"
        "WITH k(i, pnu) AS (VALUES %s)\n"
        "SELECT DISTINCT k.i FROM k WHERE EXISTS (\n"
        "  SELECT 1 FROM address a\n"
        "  WHERE a.kind = 'addr'\n"
        "    AND (a.bcode || substr(a.bd_mgt_sn, 11, 9)) = k.pnu)"
        % _values(n)
    )


def sql_road_bm25_batch(n):
    """건물관리번호 25 자리 완전 일치(A25).

    `bd_mgt_sn` 단독 색인은 없다(§1.8 실측). 색인이 있는 합성 PNU 표현식으로
    먼저 좁힌 뒤 25 자리를 재검사한다 — 같은 필지의 건물은 소수라 후속 필터가 싸다.
    """
    return (
        "/* t046:road_bm25 */\n"
        "WITH k(i, pnu, bm25) AS (VALUES %s)\n"
        "SELECT DISTINCT k.i FROM k WHERE EXISTS (\n"
        "  SELECT 1 FROM address a\n"
        "  WHERE a.kind = 'addr'\n"
        "    AND (a.bcode || substr(a.bd_mgt_sn, 11, 9)) = k.pnu\n"
        "    AND a.bd_mgt_sn = k.bm25)"
        % _values(n)
    )


def sql_referee_parcel_batch(n):
    """심판(§4.3-d) — 좌표가 그 필지 폴리곤 **안**에 있는가.

    `parcel.geom` 은 SRID 4326 이다(§1.8 실측) — 변환하지 않는다.
    `geom_pt` 는 전량 NULL 이라 쓸 수 없다.
    행이 없는 키는 결과에서 빠지고, 호출자는 그것을 **자료 부재(None)** 로 읽는다.

    F-ri — 이전 판은 8 자리 `emd_cd` 로 조인해 리를 버렸고, 그래서 `bool_or`
    가 "같은 읍면동 안 **아무** 동일번지 필지에 들어가는가"를 답했다. 심판이
    답해야 할 질문이 아니다. UNIQUE 색인 `(sido_cd, pnu)` 로 **질의 필지 한
    행만** 문다. 그러면 `bool_or` 는 사실상 그 한 필지의 판정이 된다.
    """
    return (
        "/* t046:referee */\n"
        "WITH k(i, sido_cd, pnu, lon, lat) AS (VALUES %s)\n"
        "SELECT k.i, bool_or(ST_Contains(p.geom,\n"
        "         ST_SetSRID(ST_MakePoint(k.lon, k.lat), 4326)))\n"
        "FROM k JOIN parcel p\n"
        "  ON p.sido_cd = k.sido_cd AND p.pnu = k.pnu\n"
        "GROUP BY k.i"
        % _values(n)
    )


def sql_repr_point_batch(n):
    """필지 **대표점** — 역방향 재측정의 질의 좌표(지번 층).

    왜 필요한가. 원본 판정에 쓴 VWorld 순방향 좌표는 판정 레코드에 남지 않았고
    (§3.3 이 좌표를 의도적으로 버린다) `--diag` 도 쓰이지 않아 진단 파일 자체가
    없다. 순방향 재호출은 예산 보호로 금지다. 그래서 태스크가 허용한 "표본에서
    재유도"를 택한다 — **표본 PNU 의 필지 폴리곤 대표점**이다.

    `geom_pt` 는 전량 NULL 이다(§1.8 실측). `ST_Centroid` 는 오목한 필지에서
    폴리곤 밖으로 나갈 수 있으므로 `ST_PointOnSurface` 를 쓴다 — 반드시 안이다.
    `geom` 은 SRID 4326 이라 변환하지 않는다.

    조인은 심판 축과 같은 `(sido_cd, pnu)` UNIQUE 경로다. 리를 버리는 8 자리
    키로 조인하면 이웃 리의 대표점을 질의 좌표로 삼게 된다(F-ri 와 같은 결함).
    """
    return (
        "/* t046:reprpt */\n"
        "WITH k(i, sido_cd, pnu) AS (VALUES %s)\n"
        "SELECT k.i, ST_X(ST_PointOnSurface(p.geom)),\n"
        "            ST_Y(ST_PointOnSurface(p.geom))\n"
        "FROM k JOIN parcel p\n"
        "  ON p.sido_cd = k.sido_cd AND p.pnu = k.pnu"
        % _values(n)
    )


# ── 오라클 ────────────────────────────────────────────────────────────
class Oracle:
    """배치 semi-join 으로 존재 여부를 판정한다.

    `runner(sql) -> [[셀, …], …]`. 기본은 `pgprobe.run_sql`.
    """

    def __init__(self, runner=None):
        self._run = runner or pgprobe.run_sql
        self.relax12_hits = 0
        self.relax12_attempts = 0
        # 완화로 건진 **키**. 집계층이 "완화가 없었다면" 수치를 건별로 재구성한다
        # (§8-10 엄격/완화 병기). 총계만으로는 어느 건이 완화 산물인지 알 수 없다.
        self.relax12_keys = set()
        # 조건 1-b — 구 시도코드 보정. relax12 와 **섞지 않는다**. 원인이 다르고
        # (원천-DB 코드 세대 불일치 대 행정구역 통합) 수선 주체도 다르다.
        self.legacy_hits = 0
        self.legacy_attempts = 0
        self.legacy_keys = set()
        # F2 — 심판·`O_apx` 축의 같은 보정. **지번 축과 합산하지 않는다**:
        # §7.6 이 이미 보고한 1,560 시도 / 1,546 적중은 지번·도로 축만의 수치라
        # 여기에 섞으면 이미 발표한 숫자의 의미가 사후에 바뀐다.
        self.legacy_apx_hits = 0
        self.legacy_apx_attempts = 0
        self.legacy_apx_keys = set()
        self.legacy_referee_hits = 0
        self.legacy_referee_attempts = 0
        self.legacy_referee_keys = set()
        # F1 — 대표점 축. 이 축도 **같은 보정을 통과해야 한다.** 보정 없는
        # 제 3 의 축을 새로 만드는 것이 F2 가 지적한 결함 그 자체다.
        self.legacy_reprpt_hits = 0
        self.legacy_reprpt_attempts = 0
        self.legacy_reprpt_keys = set()
        self.queries = 0

    # -- 내부 --------------------------------------------------------
    @staticmethod
    def _keymap(keys):
        """`{문자열키: 원래키}`. 충돌하면 결과가 엉뚱한 키에 붙으므로 죽인다."""
        out = {}
        for k in keys:
            s = str(k)
            if s in out:
                raise ValueError("키가 문자열로 충돌한다: %r vs %r" % (k, out[s]))
            out[s] = k
        return out

    def _exec(self, sql_template, rows):
        """자리표시자에 리터럴 행을 채워 실행한다."""
        if not rows:
            return []
        self.queries += 1
        return self._run(sql_template % tuple(rows))

    @staticmethod
    def _row_parcel(i, pnu, first):
        """`split_pnu` 로 형식을 **검증한 뒤** 정확 PNU 로 묶는다.

        `parcel.pnu` 는 text 다(실측) — char 로 캐스트하면 공백 채움 규칙이
        끼어들 수 있으므로 `::text` 로 맞춘다.
        """
        p = split_pnu(pnu)
        if first:
            return "(%s::text,%s::char(2),%s::text)" % (
                _lit(i), _lit(p["sido_cd"]), _lit(p["pnu"]))
        return "(%s,%s,%s)" % (_lit(i), _lit(p["sido_cd"]), _lit(p["pnu"]))

    @staticmethod
    def _row_apx(i, pnu, first):
        p = split_pnu(pnu)
        if first:
            return ("(%s::text,%s::char(2),%s::char(8),%s::text,"
                    "%d::smallint,%d::int)" % (
                        _lit(i), _lit(p["sido_cd"]), _lit(p["emd_cd"]),
                        _lit(p["bcode"]), p["san"], p["ji_main"]))
        return "(%s,%s,%s,%s,%d,%d)" % (
            _lit(i), _lit(p["sido_cd"]), _lit(p["emd_cd"]), _lit(p["bcode"]),
            p["san"], p["ji_main"])

    def _hit_keys(self, sql_template, rows, keymap):
        """첫 셀을 키로 읽어 적중 집합을 만든다."""
        hits = set()
        for row in self._exec(sql_template, rows):
            s = row[0]
            if s in keymap:
                hits.add(keymap[s])
        return hits

    # -- address / parcel 존재 --------------------------------------
    def _addr_hits(self, items):
        """`items` = [(키, pnu), …] → 적중 키 집합."""
        if not items:
            return set()
        keymap = self._keymap(k for k, _ in items)
        rows = [
            "(%s::text,%s::text)" % (_lit(k), _lit(pnu)) if n == 0
            else "(%s,%s)" % (_lit(k), _lit(pnu))
            for n, (k, pnu) in enumerate(items)
        ]
        return self._hit_keys(sql_addr_batch(len(rows)), rows, keymap)

    def _parcel_hits(self, items):
        if not items:
            return set()
        keymap = self._keymap(k for k, _ in items)
        rows = [self._row_parcel(k, pnu, n == 0)
                for n, (k, pnu) in enumerate(items)]
        return self._hit_keys(sql_parcel_batch(len(rows)), rows, keymap)

    def _bm25_hits(self, items):
        """`items` = [(키, pnu, bm25), …]."""
        if not items:
            return set()
        keymap = self._keymap(k for k, _, _ in items)
        rows = [
            "(%s::text,%s::text,%s::text)" % (_lit(k), _lit(p), _lit(b)) if n == 0
            else "(%s,%s,%s)" % (_lit(k), _lit(p), _lit(b))
            for n, (k, p, b) in enumerate(items)
        ]
        return self._hit_keys(sql_road_bm25_batch(len(rows)), rows, keymap)

    # -- 공개 API ----------------------------------------------------
    def jibun_batch(self, keys, relax12=True, legacy=True):
        """§4.3-b — 지번 3 분기. `{키: 'A'|'P'|'N'}`.

        미적중 키도 `'N'` 으로 남긴다. 조용히 빠지면 분모가 틀어진다.
        A 가 P 보다 우선한다 — 분기는 배타적이어야 한다.
        """
        out = {k: "N" for k in keys}
        if not keys:
            return out

        pending = list(keys.items())
        for k in self._addr_hits(pending):
            out[k] = "A"
        rest = [(k, p) for k, p in pending if out[k] == "N"]
        for k in self._parcel_hits(rest):
            out[k] = "P"

        if relax12:
            self._apply_relax12(keys, out)
        if legacy:
            self._apply_legacy(keys, out)
        return out

    def _apply_legacy(self, keys, out, road=False):
        """조건 1-b — 미적중 + 구 시도코드(42·45)인 키만 현행 코드로 재조회한다.

        `road=True` 면 BM25 25 자리도 함께 치환해 A25 분기를 살린다 —
        BM25 앞 19 자리가 PNU 이므로 접두 2 자리 치환이 그대로 성립한다.
        """
        targets = []
        for k in keys:
            if out[k] != "N":
                continue
            pnu = keys[k][1] if road else keys[k]
            cands = sido_legacy_candidates(pnu)
            if cands:
                targets.append((k, cands[0]))
        if not targets:
            return
        self.legacy_attempts += len(targets)

        if road:
            bm = {k: str(keys[k][0]) for k, _ in targets}
            triples = [(k, p, SIDO_LEGACY[str(keys[k][1])[:2]][0] + bm[k][2:])
                       for k, p in targets if bm[k]]
            for k in self._bm25_hits(triples):
                out[k] = "A25"
            rest = [(k, p) for k, p in targets if out[k] == "N"]
            for k in self._addr_hits(rest):
                out[k] = "A19"
        else:
            rest = targets
            for k in self._addr_hits(rest):
                out[k] = "A"

        rest = [(k, p) for k, p in targets if out[k] == "N"]
        for k in self._parcel_hits(rest):
            out[k] = "P"

        for k, _ in targets:
            if out[k] != "N":
                self.legacy_hits += 1
                self.legacy_keys.add(k)

    def _apply_relax12(self, keys, out):
        """조건 1 — 미적중 + 접두 12 인 키만 46 → 29 순으로 재조회한다."""
        targets = [(k, sido_relax_candidates(keys[k]))
                   for k in keys if out[k] == "N"]
        targets = [(k, c) for k, c in targets if c]
        if not targets:
            return
        self.relax12_attempts += len(targets)

        remaining = targets
        for depth in range(len(SIDO12_PARENTS)):
            probe = [(k, cands[depth]) for k, cands in remaining]
            hits_a = self._addr_hits(probe)
            rest = [(k, p) for k, p in probe if k not in hits_a]
            hits_p = self._parcel_hits(rest)
            for k in hits_a:
                out[k] = "A"
            for k in hits_p:
                out[k] = "P"
            self.relax12_hits += len(hits_a) + len(hits_p)
            self.relax12_keys.update(hits_a)
            self.relax12_keys.update(hits_p)
            remaining = [(k, c) for k, c in remaining if out[k] == "N"]
            if not remaining:
                break

    def road_batch(self, keys, legacy=True):
        """§4.3-c — 도로명 4 분기. `keys` = `{키: (bm25, pnu)}` → `{키: 'A25'|'A19'|'P'|'N'}`."""
        out = {k: "N" for k in keys}
        if not keys:
            return out

        triples = [(k, pnu, bm25) for k, (bm25, pnu) in keys.items()]
        for k in self._bm25_hits(triples):
            out[k] = "A25"

        rest = [(k, pnu) for k, pnu, _ in triples if out[k] == "N"]
        for k in self._addr_hits(rest):
            out[k] = "A19"

        rest = [(k, pnu) for k, pnu in rest if out[k] == "N"]
        for k in self._parcel_hits(rest):
            out[k] = "P"

        if legacy:
            self._apply_legacy(keys, out, road=True)
        return out

    def _apx_hits(self, items):
        """`items` = [(키, pnu), …] → 적중 키 집합."""
        if not items:
            return set()
        keymap = self._keymap(k for k, _ in items)
        rows = [self._row_apx(k, pnu, n == 0) for n, (k, pnu) in enumerate(items)]
        return self._hit_keys(sql_apx_batch(len(rows)), rows, keymap)

    def apx_batch(self, keys, legacy=True):
        """본번 근사 — `{키: bool}`. 미적중 키도 False 로 남긴다."""
        out = {k: False for k in keys}
        if not keys:
            return out
        for k in self._apx_hits(list(keys.items())):
            out[k] = True
        if legacy:
            self._apply_legacy_apx(keys, out)
        return out

    def _apply_legacy_apx(self, keys, out):
        """F2 — `O_apx` 축의 구 시도코드 보정.

        B 단계는 지번·도로 축에만 보정을 걸어 이 축을 빠뜨렸다. 무보정이면
        강원·전북 구코드 건이 근사조차 미적중(False)이 되어 **분류가 우리에게
        불리한 쪽으로** 흐른다. 방향이 보수적이라 해도 사실이 아닌 것은 같다.
        """
        targets = []
        for k in keys:
            if out[k]:
                continue
            cands = sido_legacy_candidates(keys[k])
            if cands:
                targets.append((k, cands[0]))
        if not targets:
            return
        self.legacy_apx_attempts += len(targets)
        for k in self._apx_hits(targets):
            out[k] = True
            self.legacy_apx_hits += 1
            self.legacy_apx_keys.add(k)

    def _referee_rows(self, items):
        """`items` = [(키, (pnu, lon, lat)), …] → `{키: True|False}`.

        **자료 부재 키는 아예 담기지 않는다.** 호출자가 `None` 을 유지하도록
        빈자리로 돌려주는 편이, 여기서 `None` 을 채워 넣는 것보다 안전하다.
        """
        got = {}
        if not items:
            return got
        keymap = self._keymap(k for k, _ in items)
        rows = []
        for n, (k, (pnu, lon, lat)) in enumerate(items):
            p = split_pnu(pnu)
            if n == 0:
                rows.append(
                    "(%s::text,%s::char(2),%s::text,"
                    "%.7f::double precision,%.7f::double precision)"
                    % (_lit(k), _lit(p["sido_cd"]), _lit(p["pnu"]),
                       float(lon), float(lat)))
            else:
                rows.append(
                    "(%s,%s,%s,%.7f,%.7f)"
                    % (_lit(k), _lit(p["sido_cd"]), _lit(p["pnu"]),
                       float(lon), float(lat)))

        for row in self._exec(sql_referee_parcel_batch(len(rows)), rows):
            s = row[0]
            if s in keymap and row[1] is not None:
                got[keymap[s]] = (row[1] == "t")
        return got

    def referee_parcel_batch(self, keys, legacy=True):
        """§4.3-d 심판 — `keys` = `{키: (pnu, lon, lat)}` → `{키: True|False|None}`.

        `None` 은 **심판 자료 부재**다. False(밖에 있다)와 전혀 다른 사실이며,
        조건 4 에 따라 분류 11 로 간다. 뭉개면 7/10 으로 잘못 흘러간다.
        """
        out = {k: None for k in keys}
        if not keys:
            return out
        out.update(self._referee_rows(list(keys.items())))
        if legacy:
            self._apply_legacy_referee(keys, out)
        return out

    # -- 대표점(F1) --------------------------------------------------
    def _repr_point_rows(self, items):
        """`items` = [(키, pnu), …] → `{키: (lon, lat)}`.

        **psql 은 모든 열을 문자열로 돌려준다**(실측). 여기서 float 로 바꾸지
        않으면 좌표가 문자열인 채 URL 포맷에 들어가 조용히 깨진다. 숫자로
        읽히지 않는 행은 **버린다** — 0.0 으로 뭉개면 좌표가 기니만으로 간다.
        """
        got = {}
        if not items:
            return got
        keymap = self._keymap(k for k, _ in items)
        rows = [self._row_parcel(k, pnu, n == 0)
                for n, (k, pnu) in enumerate(items)]
        for row in self._exec(sql_repr_point_batch(len(rows)), rows):
            s = row[0]
            if s not in keymap or row[1] is None or row[2] is None:
                continue
            try:
                got[keymap[s]] = (float(row[1]), float(row[2]))
            except (TypeError, ValueError):
                continue
        return got

    def repr_point_batch(self, keys, legacy=True):
        """`keys` = `{키: pnu}` → `{키: (lon, lat)}`. **부재 키는 담기지 않는다.**

        호출자는 빠진 키를 '기준 좌표 없음'으로 읽고 그 층의 커버리지에
        기록해야 한다. 조용히 채우면 측정 대상이 아닌 지점을 재는 셈이 된다.
        """
        if not keys:
            return {}
        out = self._repr_point_rows(list(keys.items()))
        if legacy:
            self._apply_legacy_reprpt(keys, out)
        return out

    def _apply_legacy_reprpt(self, keys, out):
        """F1·F2 — 대표점 축의 구 시도코드 보정. 심판 축과 같은 규칙이다."""
        targets = []
        for k in keys:
            if k in out:
                continue
            cands = sido_legacy_candidates(keys[k])
            if cands:
                targets.append((k, cands[0]))
        if not targets:
            return
        self.legacy_reprpt_attempts += len(targets)
        for k, v in self._repr_point_rows(targets).items():
            out[k] = v
            self.legacy_reprpt_hits += 1
            self.legacy_reprpt_keys.add(k)

    def _apply_legacy_referee(self, keys, out):
        """F2 — 심판 축의 구 시도코드 보정.

        **`False` 를 `True` 로 부풀리지 않는다.** 보정으로 폴리곤을 찾았는데
        점이 그 밖이면 그것이 사실이다. 여기서 말하는 "적중"은 *자료를 찾았다*
        이지 *안에 있다*가 아니다 — 둘을 뭉개면 심판이 심판이 아니게 된다.
        """
        targets = []
        for k in keys:
            if out[k] is not None:
                continue
            pnu, lon, lat = keys[k]
            cands = sido_legacy_candidates(pnu)
            if cands:
                targets.append((k, (cands[0], lon, lat)))
        if not targets:
            return
        self.legacy_referee_attempts += len(targets)
        for k, v in self._referee_rows(targets).items():
            out[k] = v
            self.legacy_referee_hits += 1
            self.legacy_referee_keys.add(k)
