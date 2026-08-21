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
    "split_pnu", "sido_relax_candidates",
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
    """PNU → 조회 컬럼. **SQL 이 아니라 여기서** 자른다(파티션 pruning)."""
    p = str(pnu)
    if len(p) != PNU_LEN:
        raise ValueError("PNU 가 19 자리가 아니다: %r" % (pnu,))
    return {
        "sido_cd": p[:2],
        "emd_cd": p[:8],
        "san": int(p[10]) - 1,        # '1'→0, '2'→1 (parcel.san 은 smallint 0/1)
        "ji_main": int(p[11:15]),
        "ji_sub": int(p[15:19]),
    }


def sido_relax_candidates(pnu):
    """조건 1 — 접두 12 를 46 → 29 순으로 확장한다. 나머지 17 자리는 보존."""
    p = str(pnu)
    if len(p) != PNU_LEN or p[:2] != SIDO12:
        return []
    return [parent + p[2:] for parent in SIDO12_PARENTS]


# ── SQL 생성 ──────────────────────────────────────────────────────────
def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def _values(n):
    """n 행짜리 자리표시자. 실제 리터럴은 `Oracle` 이 채운다."""
    return ",".join(["%s"] * max(int(n), 0))


def sql_parcel_batch(n):
    """지적 필지 존재(O=P). 파싱된 5 컬럼을 그대로 대조한다."""
    return (
        "/* t046:parcel */\n"
        "WITH k(i, sido_cd, emd_cd, san, ji_main, ji_sub) AS (VALUES %s)\n"
        "SELECT DISTINCT k.i FROM k WHERE EXISTS (\n"
        "  SELECT 1 FROM parcel p\n"
        "  WHERE p.sido_cd = k.sido_cd AND p.emd_cd = k.emd_cd\n"
        "    AND p.san = k.san AND p.ji_main = k.ji_main AND p.ji_sub = k.ji_sub)"
        % _values(n)
    )


def sql_apx_batch(n):
    """본번 근사(`O_apx`) — 부번을 보지 않는다. '본번은 있는데 부번이 없다'를 가른다."""
    return (
        "/* t046:apx */\n"
        "WITH k(i, sido_cd, emd_cd, san, ji_main) AS (VALUES %s)\n"
        "SELECT DISTINCT k.i FROM k WHERE EXISTS (\n"
        "  SELECT 1 FROM parcel p\n"
        "  WHERE p.sido_cd = k.sido_cd AND p.emd_cd = k.emd_cd\n"
        "    AND p.san = k.san AND p.ji_main = k.ji_main)"
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
    """
    return (
        "/* t046:referee */\n"
        "WITH k(i, sido_cd, emd_cd, san, ji_main, ji_sub, lon, lat) AS (VALUES %s)\n"
        "SELECT k.i, bool_or(ST_Contains(p.geom,\n"
        "         ST_SetSRID(ST_MakePoint(k.lon, k.lat), 4326)))\n"
        "FROM k JOIN parcel p\n"
        "  ON p.sido_cd = k.sido_cd AND p.emd_cd = k.emd_cd\n"
        " AND p.san = k.san AND p.ji_main = k.ji_main AND p.ji_sub = k.ji_sub\n"
        "GROUP BY k.i"
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
        p = split_pnu(pnu)
        if first:
            return "(%s::text,%s::char(2),%s::char(8),%d::smallint,%d::int,%d::int)" % (
                _lit(i), _lit(p["sido_cd"]), _lit(p["emd_cd"]),
                p["san"], p["ji_main"], p["ji_sub"])
        return "(%s,%s,%s,%d,%d,%d)" % (
            _lit(i), _lit(p["sido_cd"]), _lit(p["emd_cd"]),
            p["san"], p["ji_main"], p["ji_sub"])

    @staticmethod
    def _row_apx(i, pnu, first):
        p = split_pnu(pnu)
        if first:
            return "(%s::text,%s::char(2),%s::char(8),%d::smallint,%d::int)" % (
                _lit(i), _lit(p["sido_cd"]), _lit(p["emd_cd"]), p["san"], p["ji_main"])
        return "(%s,%s,%s,%d,%d)" % (
            _lit(i), _lit(p["sido_cd"]), _lit(p["emd_cd"]), p["san"], p["ji_main"])

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
    def jibun_batch(self, keys, relax12=True):
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
        return out

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

    def road_batch(self, keys):
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
        return out

    def apx_batch(self, keys):
        """본번 근사 — `{키: bool}`. 미적중 키도 False 로 남긴다."""
        out = {k: False for k in keys}
        if not keys:
            return out
        items = list(keys.items())
        keymap = self._keymap(k for k, _ in items)
        rows = [self._row_apx(k, pnu, n == 0) for n, (k, pnu) in enumerate(items)]
        for k in self._hit_keys(sql_apx_batch(len(rows)), rows, keymap):
            out[k] = True
        return out

    def referee_parcel_batch(self, keys):
        """§4.3-d 심판 — `keys` = `{키: (pnu, lon, lat)}` → `{키: True|False|None}`.

        `None` 은 **심판 자료 부재**다. False(밖에 있다)와 전혀 다른 사실이며,
        조건 4 에 따라 분류 11 로 간다. 뭉개면 7/10 으로 잘못 흘러간다.
        """
        out = {k: None for k in keys}
        if not keys:
            return out

        items = list(keys.items())
        keymap = self._keymap(k for k, _ in items)
        rows = []
        for n, (k, (pnu, lon, lat)) in enumerate(items):
            p = split_pnu(pnu)
            if n == 0:
                rows.append(
                    "(%s::text,%s::char(2),%s::char(8),%d::smallint,%d::int,%d::int,"
                    "%.7f::double precision,%.7f::double precision)"
                    % (_lit(k), _lit(p["sido_cd"]), _lit(p["emd_cd"]), p["san"],
                       p["ji_main"], p["ji_sub"], float(lon), float(lat)))
            else:
                rows.append(
                    "(%s,%s,%s,%d,%d,%d,%.7f,%.7f)"
                    % (_lit(k), _lit(p["sido_cd"]), _lit(p["emd_cd"]), p["san"],
                       p["ji_main"], p["ji_sub"], float(lon), float(lat)))

        for row in self._exec(sql_referee_parcel_batch(len(rows)), rows):
            s = row[0]
            if s in keymap:
                out[keymap[s]] = (row[1] == "t") if row[1] is not None else None
        return out
