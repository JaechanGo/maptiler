#!/usr/bin/env python3
"""T043 — `09-gen-geocode.py` 원천 설계 PK6 조인 교체 · 빌드 안전 게이트 단위시험.

stdlib `unittest` 만 쓴다(이 호스트에 pytest 미설치).
대상 파일명이 `09-` 로 시작해 통상 import 가 불가능하므로 `importlib` 로 적재한다.

실행:  python3 scripts/test_gen_geocode_pk.py
"""
import argparse
import importlib.util
import io
import os
import pathlib
import pwd
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
BASE_COMMIT = "4dace2b"          # T043 착수 기준 커밋 — T13 지번 조립 등가 대조용


def load(path, name="gg"):
    """숫자로 시작하는 파일명을 모듈로 적재한다.

    [T043 검수 Minor-9] `sys.dont_write_bytecode = True` 가 반드시 먼저다.
    SourceFileLoader 의 캐시 무효화는 (초 단위 mtime, 파일 크기)만 본다. 변이 시험처럼
    1초 안에 같은 크기로 바꿔치기하면 **디스크의 새 소스 대신 낡은 .pyc 가 실행된다** —
    실제로 이 함정 때문에 검수 측정이 두 라운드 폐기됐다. 캐시를 아예 만들지 않아 막는다.
    """
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


GG = load(HERE / "09-gen-geocode.py")


# ── 원천 행 합성 ──────────────────────────────────────────────────────────────
# 실제 원천 레코드 값은 싣지 않는다(docs/원천-202607-취급방침.md 판정 B).
# 아래 값은 필드 **위치**만 재현하는 합성 더미다.

def jibun_row(bcode="4311025300", emd="가덕면", ri="계산리", san="0", bon="123", bu="4",
              rncode="431102345678", jiha="0", bbon="12", bbu="0", seq="0",
              mgt="4311025300100012345", hjd="4311025301", ncols=20):
    """match_jibun 20열. c[0]=법정동코드 c[3]=읍면동 c[4]=리 c[5]=산 c[6]=본 c[7]=부
    c[8..11]=PK1~PK4 c[12]=PK5(지번일련번호) c[18]=건물관리번호 c[19]=PK6.

    ncols 로 열 수를 바꿀 수 있다 — 길이 가드(`len(c) < 20`)의 **경계**를 시험하기 위함이다.
    직전 판은 항상 정확히 20열만 만들어, 가드를 19 로 바꿔도 아무 시험이 죽지 않았다(검수 M-2).
    ncols<20 이면 뒤에서 자르고, >20 이면 빈 열을 덧댄다."""
    c = [""] * 20
    c[0] = bcode; c[3] = emd; c[4] = ri; c[5] = san; c[6] = bon; c[7] = bu
    c[8] = rncode; c[9] = jiha; c[10] = bbon; c[11] = bbu; c[12] = seq
    c[18] = mgt; c[19] = hjd
    c = c[:ncols] if ncols <= 20 else c + [""] * (ncols - 20)
    return "|".join(c)


def build_row(hjd="4311025301", sido="충청북도", sgg="청주시 상당구", emd="가덕면",
              rncode="431102345678", roadnm="계산로", jiha="0", bbon="12", bbu="0",
              postal="28100", mgt="4311025300100012345", bldnm="", hcode="4311056000",
              haeng="가덕면", detail="", E="1000000.0", N="1800000.0", ncols=27):
    """match_build 27열. c[0]=PK6(주소관할읍면동코드) c[4]=PK1 c[6..8]=PK2~PK4
    c[10]=건물관리번호 c[25]=E c[26]=N.  ncols: jibun_row 와 같은 취지(길이 가드 경계)."""
    c = [""] * 27
    c[0] = hjd; c[1] = sido; c[2] = sgg; c[3] = emd
    c[4] = rncode; c[5] = roadnm; c[6] = jiha; c[7] = bbon; c[8] = bbu
    c[9] = postal; c[10] = mgt; c[11] = bldnm; c[13] = hcode; c[14] = haeng
    c[19] = detail; c[25] = E; c[26] = N
    c = c[:ncols] if ncols <= 27 else c + [""] * (ncols - 27)
    return "|".join(c)


def write_cp949(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with io.open(path, "w", encoding="cp949", errors="replace", newline="\n") as f:
        for r in rows:
            f.write(r + "\n")


def new_state():
    return {"pid": 0, "seen": set(), "pk_miss": 0, "pk_dup": 0}


def fake_args(**kw):
    d = dict(out="/private/tmp/t043-ab/B.sqlite", src="/nonexistent", only=None,
             areas=None, no_areas=False, allow_gate_skip=False, t018_disposed=False,
             poi_csv_dir=None, osm="/nonexistent/osm.sqlite", source_label="unknown",
             # T043 검수 Minor-3 으로 추가된 게이트 임계 인자. None = 기본 상수 사용.
             min_rows_ratio=None, max_rows_ratio=None, min_rows_per_sido=None,
             taxonomy_out=None)
    d.update(kw)
    # main() 이 parse_args 직후에 세우는 파생 필드(--osm 을 사용자가 명시했는가).
    # 호출자가 osm= 을 준 것은 명령줄에 --osm 을 적은 것과 같은 뜻이므로 그때만 켠다.
    # 기본 fake_args() 의 osm 은 '관례 기본 경로'에 해당하므로 명시가 아니다.
    d.setdefault("osm_explicit", "osm" in kw and kw["osm"] is not None)
    return argparse.Namespace(**d)


class TmpMixin(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()


# ── T1 · PK6 인덱스 정합 ─────────────────────────────────────────────────────
class T1_Pk6Index(unittest.TestCase):
    def test_indices_match_t042(self):
        self.assertEqual(GG.PK6_JIBUN, (8, 9, 10, 11, 19))
        self.assertEqual(GG.PK6_BUILD, (4, 6, 7, 8, 0))

    def test_same_building_yields_same_key(self):
        j = jibun_row().split("|")
        b = build_row().split("|")
        self.assertEqual(GG.pk6_jibun(j), GG.pk6_build(b))
        self.assertEqual(GG.pk6_jibun(j), ("431102345678", "0", "12", "0", "4311025301"))


# ── T8 · 지번 조립 규범 ──────────────────────────────────────────────────────
class T8_AssembleJibun(unittest.TestCase):
    def test_four_combinations(self):
        mk = lambda san, bon, bu: jibun_row(san=san, bon=bon, bu=bu).split("|")
        self.assertEqual(GG.assemble_jibun(mk("0", "123", "0"), None), "가덕면 123")
        self.assertEqual(GG.assemble_jibun(mk("0", "123", "4"), None), "가덕면 123-4")
        self.assertEqual(GG.assemble_jibun(mk("1", "123", "0"), None), "가덕면 산 123")
        self.assertEqual(GG.assemble_jibun(mk("1", "123", "4"), None), "가덕면 산 123-4")

    def test_ri_prefixes_dong(self):
        c = mkc = jibun_row(san="1", bon="7", bu="2").split("|")
        self.assertEqual(GG.assemble_jibun(c, "계산리"), "가덕면 계산리 산 7-2")
        self.assertEqual(GG.assemble_jibun(c, None), "가덕면 산 7-2")

    def test_san_only_on_literal_one(self):
        # 산 여부는 c[5]=="1" 로만 판정한다. '2' 나 'Y' 는 산이 아니다.
        self.assertEqual(GG.assemble_jibun(jibun_row(san="2").split("|"), None), "가덕면 123-4")

    def test_empty_bu_is_not_joined(self):
        self.assertEqual(GG.assemble_jibun(jibun_row(bu="").split("|"), None), "가덕면 123")


# ── T2·T3·T4·T5·T6 · load_jibun ─────────────────────────────────────────────
class T2_LoadJibun(TmpMixin):
    def test_only_representative_jibun_enters_dict(self):
        write_cp949(self.tmp / "match_jibun_x.txt", [
            jibun_row(seq="0", bon="100", mgt="A"),
            jibun_row(seq="1", bon="200", mgt="B", rncode="431102345679"),
        ])
        st = new_state()
        jd, rid, bcd = GG.load_jibun(self.tmp, "x", st)
        self.assertEqual(len(jd), 1)
        self.assertEqual(list(jd.values()), ["가덕면 계산리 100-4"])

    def test_three_dicts_keyed_by_pk6(self):
        write_cp949(self.tmp / "match_jibun_x.txt", [jibun_row()])
        st = new_state()
        jd, rid, bcd = GG.load_jibun(self.tmp, "x", st)
        k = ("431102345678", "0", "12", "0", "4311025301")
        self.assertEqual(set(jd), {k})
        self.assertEqual(rid[k], "계산리")
        self.assertEqual(bcd[k], "4311025300")        # 변경 B: 지번측 c[0](법정동코드)

    def test_length_guard_boundary_19_20_21(self):
        """`len(c) < 20` 의 **경계**를 판정한다(검수 M-2).

        c[19]=PK6 을 읽으므로 19열은 부족하고 20열이 최소다. 직전 판의 시험은 더미 행을
        항상 정확히 20열로 만들어, 가드를 `< 19` 로 바꿔도 죽는 시험이 없었다.
        여기서는 19·20·21열을 각각 넣어 '19는 버려지고 20부터 들어온다'를 못박는다.
        """
        for ncols, want in ((19, 0), (20, 1), (21, 1)):
            with self.subTest(ncols=ncols):
                p = self.tmp / f"match_jibun_len{ncols}.txt"
                write_cp949(p, [jibun_row(ncols=ncols)])
                jd, _, _ = GG.load_jibun(self.tmp, f"len{ncols}", new_state())
                self.assertEqual(len(jd), want,
                                 f"{ncols}열 행은 {'버려져야' if want == 0 else '들어와야'} 한다")

    def test_blank_ri_becomes_none(self):
        write_cp949(self.tmp / "match_jibun_x.txt", [jibun_row(ri="   ")])
        st = new_state()
        jd, rid, bcd = GG.load_jibun(self.tmp, "x", st)
        k = next(iter(jd))
        self.assertIsNone(rid[k])
        self.assertEqual(jd[k], "가덕면 123-4")

    def test_duplicate_pk6_counted_and_first_kept(self):
        write_cp949(self.tmp / "match_jibun_x.txt", [
            jibun_row(bon="100", ri="계산리"),
            jibun_row(bon="200", ri="한계리"),          # 같은 PK6 · 다른 값
        ])
        st = new_state()
        jd, rid, bcd = GG.load_jibun(self.tmp, "x", st)
        self.assertEqual(st["pk_dup"], 1)
        self.assertEqual(list(jd.values()), ["가덕면 계산리 100-4"])   # 첫 값 유지

    def test_missing_file_returns_empty_silently(self):
        st = new_state()
        jd, rid, bcd = GG.load_jibun(self.tmp, "없는시도", st)
        self.assertEqual((jd, rid, bcd), ({}, {}, {}))     # 침묵 — G2 가 승격한다


# ── T7 · add_juso 조인 및 bcode 폴백 ────────────────────────────────────────
class T7_AddJuso(TmpMixin):
    def _db(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)                 # ResourceWarning 을 남기지 않는다
        db.executescript(GG.SCHEMA)
        return db

    def _write_pair(self, sido="chungbuk", jrows=None, brows=None):
        write_cp949(self.tmp / f"match_jibun_{sido}.txt", jrows or [jibun_row()])
        write_cp949(self.tmp / f"match_build_{sido}.txt", brows or [build_row()])

    def test_ri_and_bcode_come_from_jibun_side(self):
        self._write_pair()
        db = self._db(); st = new_state()
        GG.add_juso(db, self.tmp, {"chungbuk"}, st)
        row = db.execute("SELECT ri, bcode, jibun FROM places WHERE kind='addr'").fetchone()
        self.assertEqual(row[0], "계산리")
        self.assertEqual(row[1], "4311025300")            # 지번측 c[0]
        self.assertEqual(row[2], "가덕면 계산리 123-4")

    def test_length_guard_boundary_26_27_28(self):
        """`len(c) < 27` 의 경계. c[26]=N 을 읽으므로 27열이 최소다(검수 M-2)."""
        # 슬러그는 **실재하는 것**이어야 한다 — add_juso 는 effective_sido() 로 거르므로
        # 가짜 슬러그를 주면 루프가 아예 돌지 않아 시험이 코드를 태우지 못한다.
        for ncols, want, sido in ((26, 0, "sejong"), (27, 1, "daegu"), (28, 1, "jeju")):
            with self.subTest(ncols=ncols, sido=sido):
                self._write_pair(sido=sido, brows=[build_row(ncols=ncols)])
                db = self._db(); st = new_state()
                GG.add_juso(db, self.tmp, {sido}, st)
                n = db.execute("SELECT count(*) FROM places").fetchone()[0]
                self.assertEqual(n, want,
                                 f"{ncols}열 행은 {'버려져야' if want == 0 else '들어와야'} 한다")

    def test_pk6_miss_is_counted_and_row_dropped(self):
        self._write_pair(brows=[build_row(rncode="999999999999")])   # 조인 불성립
        db = self._db(); st = new_state()
        GG.add_juso(db, self.tmp, {"chungbuk"}, st)
        self.assertEqual(st["pk_miss"], 1)
        self.assertEqual(db.execute("SELECT count(*) FROM places").fetchone()[0], 0)

    def test_bcode_falls_back_to_build_c0_when_dict_lacks_key(self):
        # m-N7: 정상 경로에서는 도달 불가한 방어 분기다. bcd 만 비워 강제로 태운다.
        self._write_pair()
        orig = GG.load_jibun

        def fake(src, sido, state):
            jd, rid, bcd = orig(src, sido, state)
            bcd.clear()
            return jd, rid, bcd

        GG.load_jibun = fake
        try:
            db = self._db(); st = new_state()
            GG.add_juso(db, self.tmp, {"chungbuk"}, st)
        finally:
            GG.load_jibun = orig
        self.assertEqual(db.execute("SELECT bcode FROM places").fetchone()[0], "4311025301")

    def test_derive_jibun_is_gone(self):
        self.assertFalse(hasattr(GG, "_derive_jibun"))


# ── T9 · effective_sido ─────────────────────────────────────────────────────
class T9_EffectiveSido(unittest.TestCase):
    def test_sido_list_is_202607_sixteen(self):
        self.assertEqual(len(GG.SIDO), 16)
        self.assertIn("jeonnamgwangju", GG.SIDO)
        self.assertNotIn("gwangju", GG.SIDO)
        self.assertNotIn("jeonnam", GG.SIDO)

    def test_only_narrows_in_sido_order(self):
        self.assertEqual(GG.effective_sido({"sejong", "daegu"}), ["daegu", "sejong"])
        self.assertEqual(GG.effective_sido(None), GG.SIDO)


# ── T10 · G0 전국 재빌드 차단 ───────────────────────────────────────────────
class T10_FullRebuild(unittest.TestCase):
    def test_three_required_outcomes(self):
        allsido = GG.SIDO
        self.assertTrue(GG.is_full_rebuild(fake_args(out="/private/tmp/t043-ab/x.sqlite"), allsido))
        self.assertTrue(GG.is_full_rebuild(
            fake_args(out=os.path.expanduser("~/geocode-build/geocode.sqlite")), ["chungbuk"]))
        self.assertFalse(GG.is_full_rebuild(
            fake_args(out="/private/tmp/t043-ab/x.sqlite"), ["chungbuk", "daegu", "sejong"]))


class T10b_BuildHomeCannotEscape(unittest.TestCase):
    """C-N1 — BUILD_HOME 을 딴 데로 돌려도 정본 경로는 항상 정본이다."""

    def test_build_home_is_additive_not_replacing(self):
        old = os.environ.get("BUILD_HOME")
        os.environ["BUILD_HOME"] = "/private/tmp/anything"
        try:
            args = fake_args(out=os.path.expanduser("~/geocode-build/geocode.sqlite"))
            self.assertTrue(GG.is_full_rebuild(args, ["chungbuk"]))
            # BUILD_HOME 이 가리키는 곳도 함께 정본 취급된다(추가이지 대체가 아니다).
            self.assertTrue(GG.is_full_rebuild(fake_args(out="/private/tmp/anything/x.sqlite"),
                                               ["chungbuk"]))
        finally:
            if old is None:
                os.environ.pop("BUILD_HOME", None)
            else:
                os.environ["BUILD_HOME"] = old


class T10c_HomeTamperCannotEscape(unittest.TestCase):
    """C-1 — `$HOME` 을 바꿔치기해도 정본은 정본이다.

    직전 판의 `_homes()` 는 `expanduser()` 하나로 `~/geocode-build` 를 풀었다.
    `expanduser()` 는 `$HOME` 을 **그대로 믿으므로**, HOME 을 딴 데로 돌린 채
    `--out ~진짜홈/geocode-build/geocode.sqlite` 를 겨누면 G0 가 통째로 관통됐다
    (검수 실증: 전 게이트 PASS · exit 0 · 정본 자리 파일 교체).
    cron·systemd·`sudo -H`·`docker run -e HOME=` 이 전부 이 상태를 만든다.
    """

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("HOME", "BUILD_HOME")}
        self._td = tempfile.TemporaryDirectory()

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._td.cleanup()

    @staticmethod
    def _real_home():
        return pwd.getpwuid(os.getuid()).pw_dir

    def test_canonical_is_still_canonical_after_home_swap(self):
        real = pathlib.Path(self._real_home()) / "geocode-build" / "geocode.sqlite"
        os.environ["HOME"] = self._td.name                 # HOME 을 통째로 딴 데로
        os.environ.pop("BUILD_HOME", None)
        self.assertTrue(GG.targets_canonical(real))
        self.assertTrue(GG.is_full_rebuild(fake_args(out=str(real)), ["chungbuk"]))

    def test_home_swap_target_is_also_canonical(self):
        """바꿔친 HOME 쪽 ~/geocode-build 도 정본 취급된다 — 후보는 늘 뿐 줄지 않는다."""
        os.environ["HOME"] = self._td.name
        fake = pathlib.Path(self._td.name) / "geocode-build" / "geocode.sqlite"
        self.assertTrue(GG.targets_canonical(fake))

    def test_ordinary_path_stays_non_canonical(self):
        """폴백이 새 구멍이 되지 않았는가 — 평범한 경로는 여전히 정본이 아니다."""
        os.environ["HOME"] = self._td.name
        os.environ.pop("BUILD_HOME", None)
        self.assertFalse(GG.targets_canonical(pathlib.Path(self._td.name) / "work" / "x.sqlite"))
        self.assertFalse(GG.is_full_rebuild(
            fake_args(out=str(pathlib.Path(self._td.name) / "work" / "x.sqlite")), ["chungbuk"]))

    def test_relative_and_dotdot_paths_are_resolved(self):
        """상대경로·`..` 우회. 아이노드 판정이 경로 표기를 무의미하게 만든다."""
        real = pathlib.Path(self._real_home()) / "geocode-build"
        if not real.is_dir():
            self.skipTest("정본 디렉터리 부재 — 아이노드 판정을 시험할 수 없다")
        self.assertTrue(GG.targets_canonical(real / ".." / "geocode-build" / "geocode.sqlite"))

    def test_symlinked_canonical_dir_is_detected(self):
        """심볼릭링크 우회. 링크로 부르든 실경로로 부르든 (st_dev, st_ino)는 같다."""
        real = pathlib.Path(self._real_home()) / "geocode-build"
        if not real.is_dir():
            self.skipTest("정본 디렉터리 부재")
        link = pathlib.Path(self._td.name) / "alias"
        os.symlink(str(real), str(link))
        os.environ["HOME"] = self._td.name
        os.environ.pop("BUILD_HOME", None)
        self.assertTrue(GG.targets_canonical(link / "geocode.sqlite"))

    def test_missing_canonical_dir_still_blocked_by_path_fallback(self):
        """첫 빌드(정본 디렉터리 부재) — 아이노드가 못 잡는 자리를 문자열 폴백이 받는다."""
        os.environ["HOME"] = self._td.name                 # 이 안에 geocode-build 는 아직 없다
        os.environ.pop("BUILD_HOME", None)
        target = pathlib.Path(self._td.name) / "geocode-build" / "geocode.sqlite"
        self.assertFalse(target.parent.exists())
        self.assertTrue(GG.targets_canonical(target))


# ── T11 · 게이트 판정 ───────────────────────────────────────────────────────
class T11_Gates(TmpMixin):
    _seq = 0

    def _db_with(self, rows):
        T11_Gates._seq += 1                       # 한 시험에서 두 번 부르므로 파일명을 겹치지 않게 한다
        p = self.tmp / f"g{T11_Gates._seq}.sqlite"
        db = sqlite3.connect(p)
        db.executescript(GG.SCHEMA)
        for r in rows:
            db.execute("INSERT INTO places(id,kind,ri,bcode,lon,lat) VALUES(?,?,?,?,?,?)", r)
        db.commit(); db.close()
        ro = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        self.addCleanup(ro.close)                 # ResourceWarning 을 남기지 않는다
        return ro

    def test_g1_rejects_unknown_slug(self):
        r = GG.gate_args(fake_args(src=str(self.tmp)), {"chungbuk", "typo"})
        self.assertEqual(r.verdict, "FAIL")
        r2 = GG.gate_args(fake_args(src=str(self.tmp)), {"chungbuk"})
        self.assertEqual(r2.verdict, "PASS")

    def test_g2_requires_both_files(self):
        write_cp949(self.tmp / "match_build_chungbuk.txt", [build_row()])
        r = GG.gate_inputs(self.tmp, ["chungbuk"])
        self.assertEqual(r.verdict, "FAIL")           # jibun 없음
        write_cp949(self.tmp / "match_jibun_chungbuk.txt", [jibun_row()])
        self.assertEqual(GG.gate_inputs(self.tmp, ["chungbuk"]).verdict, "PASS")

    def test_g2_catches_missing_osm_only_when_asked_for(self):
        """Minor-2 — `--osm` 오타로 G3 기준선이 조용히 낮아지는 길을 막는다.

        주지 않은 경우(주소전용 빌드)는 정상이고, **주었는데 없는** 경우만 실패다.
        """
        write_cp949(self.tmp / "match_build_chungbuk.txt", [build_row()])
        write_cp949(self.tmp / "match_jibun_chungbuk.txt", [jibun_row()])
        eff = ["chungbuk"]

        # (a) --osm 없음 → 의도된 주소전용. PASS
        self.assertEqual(
            GG.gate_inputs(self.tmp, eff, fake_args(osm=None)).verdict, "PASS")

        # (b) --osm 지정 + 파일 없음 → FAIL. 기준선이 낮아지는 것을 여기서 끊는다
        bad = self.tmp / "osm-오타.sqlite"
        r = GG.gate_inputs(self.tmp, eff, fake_args(osm=str(bad)))
        self.assertEqual(r.verdict, "FAIL")
        self.assertIn("기준선", r.note)

        # (c) --osm 지정 + 파일 있음 → PASS
        bad.write_bytes(b"")
        self.assertEqual(
            GG.gate_inputs(self.tmp, eff, fake_args(osm=str(bad))).verdict, "PASS")

    def test_g2_allows_missing_default_osm_for_address_only_build(self):
        """Minor-2 의 **과잉 차단**을 막는다 — 이 시험이 없으면 조치가 정당한 빌드를 죽인다.

        `--osm` 은 미지정 시 $BUILD_HOME/osm.sqlite 를 관례로 집는다. 그 파일이 없는
        환경(첫 빌드·배포서버·CI)에서 주소전용 부분 빌드는 정당한 실행이고, G2 가 그것을
        막아서는 안 된다. 실제로 초안 조치는 argparse 기본값 때문에 이 경우까지 FAIL 로
        떨어뜨렸다.
        """
        write_cp949(self.tmp / "match_build_chungbuk.txt", [build_row()])
        write_cp949(self.tmp / "match_jibun_chungbuk.txt", [jibun_row()])
        gone = str(self.tmp / "없는-osm.sqlite")
        a = fake_args(osm=gone, osm_explicit=False,       # 관례 기본값이 집은 경로
                      out="/private/tmp/t043-ab/B.sqlite")   # 정본 아님
        r = GG.gate_inputs(self.tmp, ["chungbuk"], a)
        self.assertEqual(r.verdict, "PASS", f"주소전용 빌드를 막았다: {r.note}")
        self.assertIn("주소전용", r.actual)               # 침묵하지는 않는다

    def test_g2_requires_osm_when_the_build_targets_the_canonical_db(self):
        """정본을 겨눈 빌드는 osm 부재를 눈감아 주지 않는다 — 명시하지 않았더라도.

        정본은 osm 유래 594,704 행을 담아야 하는 산출물이다. 그것이 통째로 빠진 채
        **낮아진 기준선**으로 G3 를 통과하면 게이트가 게이트가 아니다.
        """
        write_cp949(self.tmp / "match_build_chungbuk.txt", [build_row()])
        write_cp949(self.tmp / "match_jibun_chungbuk.txt", [jibun_row()])
        canon = os.path.expanduser("~/geocode-build/geocode.sqlite")   # 읽지도 쓰지도 않는다
        self.assertTrue(GG.targets_canonical(canon), "시험 전제: 정본 경로여야 한다")
        a = fake_args(osm=str(self.tmp / "없는-osm.sqlite"),
                      osm_explicit=False, out=canon)
        r = GG.gate_inputs(self.tmp, ["chungbuk"], a)
        self.assertEqual(r.verdict, "FAIL")
        self.assertIn("정본", r.note)

    def test_missing_osm_actually_lowers_the_g3_baseline(self):
        """위 게이트가 왜 필요한지를 **수치로** 남긴다 — 기준선이 실제로 낮아진다."""
        eff = ["sejong"]
        base_addr = GG.BASELINE_ADDR["sejong"]
        have = self.tmp / "osm.sqlite"
        have.write_bytes(b"")
        g3_with, _ = GG.gate_rowcounts(base_addr + GG.BASELINE_OSM, base_addr, eff,
                                       fake_args(osm=str(have)))
        g3_without, _ = GG.gate_rowcounts(base_addr, base_addr, eff, fake_args(osm=None))
        self.assertEqual(g3_with.verdict, "PASS")
        self.assertEqual(g3_without.verdict, "PASS")
        self.assertIn("osm", g3_with.actual)          # 기준에 osm 이 들어간다
        self.assertNotIn("osm", g3_without.actual)    # 빠지면 기준이 그만큼 내려간다

    def test_every_sido_has_a_baseline(self):
        """C-2 의 뿌리 — 슬러그 16종 전부에 기준선이 있어야 비례 게이트가 산다.

        직전 판은 jeonnamgwangju 하나가 비어 있었고, 전국 재빌드는 정의상 그 시도를
        포함하므로 **G3·G4 가 항상 SKIP** 이었다. 그 상태에서 --allow-gate-skip 하나면
        0행 산출물이 정본을 덮어썼다(검수 실증: exit 0 · 총 0건 · 0MB).
        """
        self.assertEqual(sorted(GG.BASELINE_ADDR), sorted(GG.SIDO))
        self.assertTrue(all(v > 0 for v in GG.BASELINE_ADDR.values()))

    def test_zero_row_nationwide_is_blocked_even_with_allow_skip(self):
        """C-2 실증의 단위판 — 0행 전국 산출은 어떤 스위치로도 통과하지 못한다."""
        g3, g4 = GG.gate_rowcounts(0, 0, GG.SIDO, fake_args(allow_gate_skip=True))
        self.assertEqual(g3.verdict, "FAIL")
        self.assertEqual(g4.verdict, "FAIL")
        # FAIL 은 --allow-gate-skip 으로 해제되지 않는다.
        self.assertTrue(GG.gates_failed([g3, g4], allow_skip=True))

    def test_g4_fails_on_row_loss(self):
        base = GG.BASELINE_ADDR["sejong"]
        _, ok = GG.gate_rowcounts(base, base, ["sejong"], fake_args())
        self.assertEqual(ok.verdict, "PASS")
        _, bad = GG.gate_rowcounts(base // 2, base // 2, ["sejong"], fake_args())
        self.assertEqual(bad.verdict, "FAIL")

    # ---- M-2: 임계 상수 3층을 **각각 단독으로** 반증한다 --------------------
    # 직전 판은 `ok4 = 창 and 바닥` 을 한 줄로 판정해, 기본값에서 창(0.98)이 바닥(0.93)을
    # 완전히 가렸다. 그래서 GATE_FLOOR 를 0.93→0.10 으로 바꿔도, 창을 0.01~9.99 로
    # 열어젖혀도 죽는 시험이 하나도 없었다(검수 M-2 변이 3종 생존).
    # 아래 세 시험은 나머지 두 층이 반드시 통과하는 값을 골라 한 층씩만 무너뜨린다.

    def test_window_alone_can_fail(self):
        """창만 위반 — 비례바닥·절대바닥은 넉넉히 통과하는 값으로."""
        base = GG.BASELINE_ADDR["sejong"]
        n = int(base * 1.30)                       # 창 상한(1.10) 초과. 바닥은 하한이라 무관
        _, g4 = GG.gate_rowcounts(n, n, ["sejong"], fake_args())
        self.assertEqual(g4.verdict, "FAIL")
        self.assertIn("창", g4.note)
        self.assertNotIn("바닥", g4.note)          # 무너진 층은 창 하나뿐이어야 한다
        # 창을 그만큼 넓히면 통과해야 한다 — 창이 실제로 판정에 쓰인다는 증거
        _, g4b = GG.gate_rowcounts(n, n, ["sejong"], fake_args(max_rows_ratio=1.50))
        self.assertEqual(g4b.verdict, "PASS")

    def test_proportional_floor_alone_can_fail(self):
        """비례바닥(GATE_FLOOR)만 위반 — 창은 넓혀 통과시키고 절대바닥은 여유를 둔다."""
        base = GG.BASELINE_ADDR["sejong"]          # 55,846
        n = int(base * 0.50)                       # 27,923 — 절대바닥 10,000 위
        args = fake_args(min_rows_ratio=0.10)      # 창 하한을 0.10 으로 열어젖힌다
        _, g4 = GG.gate_rowcounts(n, n, ["sejong"], args)
        self.assertEqual(g4.verdict, "FAIL")
        self.assertIn("비례바닥", g4.note)
        self.assertNotIn("창", g4.note)            # 창은 통과했다
        self.assertNotIn("절대바닥", g4.note)      # 절대바닥도 통과했다
        # GATE_FLOOR 위로 올리면 같은 인자에서 통과한다
        n2 = int(base * 0.95)
        _, g4b = GG.gate_rowcounts(n2, n2, ["sejong"], args)
        self.assertEqual(g4b.verdict, "PASS")

    def test_absolute_floor_alone_can_fail(self):
        """절대바닥만 위반 — 기준선이 없어 비례 두 층이 아예 계산되지 않는 상황."""
        unknown = "sido_without_baseline"          # 미래에 시도가 늘면 실제로 생기는 상태
        self.assertNotIn(unknown, GG.BASELINE_ADDR)
        g3, g4 = GG.gate_rowcounts(0, 0, [unknown], fake_args())
        self.assertEqual(g3.verdict, "SKIP")       # 비례 판정 불가
        self.assertEqual(g4.verdict, "FAIL")       # 그래도 절대바닥은 선다
        self.assertIn("절대하한", g4.note)
        # 절대바닥 위면 SKIP 으로 내려간다(판정 불가일 뿐 실패는 아니다)
        _, g4b = GG.gate_rowcounts(99999, 99999, [unknown], fake_args())
        self.assertEqual(g4b.verdict, "SKIP")

    def test_absolute_floor_cannot_be_lowered_by_flag(self):
        """3층은 명령줄로 **올릴 수만** 있다. 낮추는 값은 무시된다."""
        lo, hi, mp = GG.gate_ratios(fake_args(min_rows_per_sido=1))
        self.assertEqual(mp, GG.GATE_MIN_PER_SIDO)
        lo, hi, mp = GG.gate_ratios(fake_args(min_rows_per_sido=999999))
        self.assertEqual(mp, 999999)
        lo, hi, mp = GG.gate_ratios(fake_args())
        self.assertEqual((lo, hi, mp), (GG.GATE_LO, GG.GATE_HI, GG.GATE_MIN_PER_SIDO))

    def test_absolute_floor_magnitude_is_pinned(self):
        """3층 절대바닥의 **크기 자체**를 못박는다 (2차 검수 N-3).

        위 시험의 단언은 `mp == GG.GATE_MIN_PER_SIDO` 라 값에 대해 **동어반복**이었다 —
        상수를 10,000 → 1 로 바꿔도 49개 시험이 전부 살았다. 상수가 1 이 되면 3층이
        시도당 1행이 되어, C-2 가 막으려던 "거의 빈 산출물"이 다시 통과한다.

        상한도 같이 건다. 상한이 없으면 **정상 빌드를 오차단하는 방향**의 회귀
        (예: 30,000)를 못 잡는다. 기준은 기준선상 최소 시도(sejong 55,846 행)다 —
        절대바닥이 그 절반을 넘으면 세종 단독 빌드가 정당한데도 막힌다.
        """
        smallest = min(GG.BASELINE_ADDR.values())          # sejong
        self.assertGreaterEqual(GG.GATE_MIN_PER_SIDO, 10_000)
        self.assertLess(GG.GATE_MIN_PER_SIDO, smallest // 2)
        # 크기가 실제로 무엇을 막는지 — 시도당 1행짜리 산출물은 반드시 FAIL 이어야 한다.
        # (상수가 1 이면 여기서 SKIP 으로 내려가 이 단언이 죽는다)
        unknown = "sido_without_baseline"
        self.assertNotIn(unknown, GG.BASELINE_ADDR)
        _g3, g4 = GG.gate_rowcounts(1, 1, [unknown], fake_args())
        self.assertEqual(g4.verdict, "FAIL")
        self.assertIn("절대하한", g4.note)

    def test_floor_is_below_default_window_by_design(self):
        """2층이 기본 창보다 아래에 있다는 사실 자체를 못박는다.

        검수 Minor-1 은 이 관계를 "GATE_FLOOR 는 사문"이라 읽었다. 사문이 아니라 **설계**다 —
        창은 인자로 넓힐 수 있고, 넓힌 순간 2층이 드러난다(위 test_proportional_floor_alone_can_fail).
        다만 코드 주석이 창을 '-2%~+10%' 로 고정 서술한 것은 거짓이었으므로 바로잡았다.
        """
        self.assertLess(GG.GATE_FLOOR, GG.GATE_LO)

    def test_g5_catches_out_of_range_coords(self):
        con = self._db_with([(1, "addr", None, "4311025300", 127.0, 36.0),
                             (2, "addr", None, "4311025300", 12.0, 36.0)])
        self.assertEqual(GG.gate_coords(con).verdict, "FAIL")

    def test_g8_g9_are_zero_tolerance(self):
        self.assertEqual(GG.gate_pk_miss({"pk_miss": 0}).verdict, "PASS")
        self.assertEqual(GG.gate_pk_miss({"pk_miss": 1}).verdict, "FAIL")
        self.assertEqual(GG.gate_pk_dup({"pk_dup": 0}).verdict, "PASS")
        self.assertEqual(GG.gate_pk_dup({"pk_dup": 1}).verdict, "FAIL")

    def test_g10_ri_iff_ri_digits(self):
        good = self._db_with([(1, "addr", "계산리", "4311025301", 127.0, 36.0),
                              (2, "addr", None, "4311025300", 127.0, 36.0),
                              # biz 는 검사 대상 밖 — bcode 없이 ri 를 갖는다
                              (3, "biz", "계산리", None, 127.0, 36.0)])
        self.assertEqual(GG.gate_ri_bcode(good).verdict, "PASS")
        bad = self._db_with([(1, "addr", "계산리", "4311025300", 127.0, 36.0)])
        self.assertEqual(GG.gate_ri_bcode(bad).verdict, "FAIL")

    def test_g10_predicate_is_symmetric_on_null_and_empty(self):
        """Minor-4 — 좌우가 NULL·빈값·짧은 값을 같은 방식으로 다룬다.

        직전 판은 ri 쪽만 `IS NOT NULL`(빈 문자열은 '리가 있다'로 셈)이고 bcode 쪽은
        `COALESCE(...,'')`(NULL 을 '리 없음'으로 셈)이라 좌우 처리가 어긋나 있었다.
        """
        # ri='' 는 '리 없음'이다 — bcode 끝 '00' 과 짝이 맞아야 한다
        self.assertEqual(GG.gate_ri_bcode(self._db_with(
            [(1, "addr", "", "4311025300", 127.0, 36.0)])).verdict, "PASS")
        # ri='' 인데 bcode 끝이 리자리를 가지면 불일치
        self.assertEqual(GG.gate_ri_bcode(self._db_with(
            [(1, "addr", "", "4311025301", 127.0, 36.0)])).verdict, "FAIL")
        # bcode 가 NULL 이면 '리 없음'. ri 도 없어야 짝이 맞는다
        self.assertEqual(GG.gate_ri_bcode(self._db_with(
            [(1, "addr", None, None, 127.0, 36.0)])).verdict, "PASS")
        self.assertEqual(GG.gate_ri_bcode(self._db_with(
            [(1, "addr", "계산리", None, 127.0, 36.0)])).verdict, "FAIL")
        # 두 자리에 못 미치는 bcode 가 우변에서 조용히 참이 되지 않는가
        self.assertEqual(GG.gate_ri_bcode(self._db_with(
            [(1, "addr", None, "4", 127.0, 36.0)])).verdict, "PASS")
        self.assertEqual(GG.gate_ri_bcode(self._db_with(
            [(1, "addr", "계산리", "4", 127.0, 36.0)])).verdict, "FAIL")


# ── T12 · SKIP 은 실패다 ────────────────────────────────────────────────────
class T12_SkipIsFailure(unittest.TestCase):
    def test_skip_counts_as_failure_unless_allowed(self):
        rs = [GG.GateResult("G3", "SKIP", "-", "-", "")]
        self.assertTrue(GG.gates_failed(rs, allow_skip=False))
        self.assertFalse(GG.gates_failed(rs, allow_skip=True))

    def test_na_never_fails(self):
        rs = [GG.GateResult("G6", "N/A", "-", "-", "")]
        self.assertFalse(GG.gates_failed(rs, allow_skip=False))

    def test_fail_is_not_released_by_allow_skip(self):
        rs = [GG.GateResult("G8", "FAIL", 3, 0, "")]
        self.assertTrue(GG.gates_failed(rs, allow_skip=True))


# ── T13 · 지번 조립 등가 (기준 커밋 대조) ───────────────────────────────────
class T13_JibunAssemblyUnchanged(TmpMixin):
    def test_jibun_strings_identical_to_base_commit(self):
        dump = self.tmp / "09-A.py"
        try:
            with open(dump, "wb") as f:
                subprocess.run(["git", "-C", str(ROOT), "show",
                                f"{BASE_COMMIT}:scripts/09-gen-geocode.py"],
                               stdout=f, check=True, stderr=subprocess.PIPE)
        except (OSError, subprocess.CalledProcessError) as e:
            raise unittest.SkipTest(f"기준 커밋 추출 불가: {e}")

        rows, mgt = [], 0
        for san in ("0", "1"):
            for bu in ("0", "7"):
                mgt += 1
                rows.append(jibun_row(san=san, bu=bu, mgt=f"MGT{mgt:016d}",
                                      rncode=f"43110234567{mgt}"))
        src = self.tmp / "src"
        write_cp949(src / "match_jibun_x.txt", rows)

        old = load(dump, "gg_old")
        d_old, _nm, _rd = old.load_jibun(src, "x")
        d_new, _rid, _bcd = GG.load_jibun(src, "x", new_state())
        self.assertEqual(len(d_old), 4)
        self.assertEqual(sorted(d_old.values()), sorted(d_new.values()))


# ── T10d · 아이노드 판정 계층 회귀 보호 (검수 N-2) ───────────────────────────
class T10d_InodeLayerIsLoadBearing(unittest.TestCase):
    """`targets_canonical()` 의 (a) 아이노드 층을 **단독으로** 반증한다.

    검수 실증: `:599` 를 `if False and _dir_id(d) in hids:` 로 바꿔도 49개 시험이
    하나도 안 죽었다. 기존 T10c 7건이 전부 (b) 경로문자열 층으로 통과하기 때문이다.
    그러나 (a)는 사문이 아니다 — macOS **firmlink**(`/System/Volumes/Data/...`)는
    `Path.resolve()` 가 접지 못해 **문자열은 다르고 아이노드만 같다**.

    **하중은 마지막 두 단언에 있다.** m1(`if False and _dir_id(d) in hids:`)을 죽이는
    것은 그 둘이다. 3차 검수가 첫 단언을 지운 채 m1 을 걸어 **여전히 KILLED** 임을
    실측했고, 4차에서 재현했다 — 실패 지점은 `assertTrue(targets_canonical(out))` 이다.

    첫 단언은 하중이 아니라 **층 강등 카나리아**다. `_home_dirs()` 가 넓어져 firmlink 를
    (b) 경로문자열 층으로도 잡게 되면, 이 시험은 **초록인 채로** 아이노드 층 검증을
    잃는다. 그때 첫 단언이 발화해 그 사실을 알린다 — 없어도 m1 은 잡히지만, 없으면
    이 시험이 언제 (b) 층 시험으로 강등됐는지 아무도 모른다.
    """

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("HOME", "BUILD_HOME")}
        os.environ.pop("BUILD_HOME", None)      # 정본 판정에 BUILD_HOME 이 끼지 않게 한다

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_inode_layer_alone_catches_firmlink(self):
        canon = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir) / "geocode-build"
        if not canon.is_dir():
            self.skipTest("정본 디렉터리 없음 — firmlink 를 걸 대상이 없다")
        fl = pathlib.Path("/System/Volumes/Data") / canon.relative_to("/")
        if not fl.is_dir():
            self.skipTest("firmlink 없음(비 macOS)")
        os.environ["HOME"] = str(canon.parent)
        out = str(fl / "geocode.sqlite")

        # ── 카나리아(하중 아님): (b) 경로문자열 층으로는 못 잡는다 ───────────
        #    지워도 m1 은 여전히 잡힌다. 이 시험이 (b) 층 시험으로 강등되는
        #    순간을 알리는 장치다 — 3차 검수 M-A 로 서술을 바로잡았다.
        base = GG._abspath(out).parent
        chain = {base, *base.parents}
        hstr = {GG._abspath(h) for h in GG._home_dirs()}
        self.assertFalse(chain & hstr,
                         "firmlink 가 문자열로도 잡히면 이 시험은 아이노드 층을 검증하지 못한다")
        # 실체는 같은 디렉터리다 — 그래서 (a)가 잡아야 한다
        self.assertEqual(GG._dir_id(fl), GG._dir_id(canon))

        # ── 하중: 그런데도 정본으로 판정된다 → (a) 아이노드 층이 한 일이다 ───
        #    m1 을 죽이는 것은 아래 두 줄이다.
        self.assertTrue(GG.targets_canonical(out))
        self.assertTrue(GG.is_full_rebuild(fake_args(out=out, only="sejong"), ["sejong"]))


# ── T14 · 경로 인자 정규화 (검수 N-1) ────────────────────────────────────────
class T14_ArgPathNormalization(TmpMixin):
    """리터럴 틸데 입력에서 소비 지점마다 술어가 갈리던 것을 **1회 정규화**로 봉한다.

    검수 실증: `--osm ~/geocode-build/osm.sqlite`(셸이 확장하지 않은 리터럴 틸데)를 주면
    `gate_inputs` 는 `expanduser().exists()` 로 "있다", `gate_rowcounts` 와 `add_osm` 은
    `Path(x).exists()` 로 "없다"로 갈린다. 결과: osm 594,704 행이 통째로 빠진 산출물이
    11개 게이트를 전부 통과한다(검수 측정 n_places 621,383 · G2·G3·G4 모두 PASS).

    Docker `-e BUILD_HOME=~/...`, systemd `Environment=`, JSON/YAML 설정이 전부
    이 상태를 만든다 — 셸을 거치지 않으므로 틸데가 그대로 남는다.

    아래 시험들의 **하중은 "정규화 전에는 갈린다"를 먼저 못박는 데** 있다. 그 전제가
    재현되지 않으면 정규화 후의 '일치' 단언은 아무것도 검증하지 않는다.
    """

    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.get(k) for k in ("HOME", "BUILD_HOME")}

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def _fake_home_with_osm(self):
        """HOME 을 임시 디렉터리로 돌리고 그 아래 **실재하는** osm.sqlite 를 만든다."""
        home = self.tmp / "home"
        bd = home / "geocode-build"
        bd.mkdir(parents=True)
        p = bd / "osm.sqlite"
        db = sqlite3.connect(p)
        db.execute("CREATE TABLE places(name TEXT,type TEXT,subtype TEXT,lon REAL,lat REAL)")
        db.executemany("INSERT INTO places VALUES(?,?,?,?,?)",
                       [("가락시장역", "station", "subway", 127.11, 37.49),
                        ("한강공원", "park", "park", 127.12, 37.50)])
        db.commit(); db.close()
        os.environ["HOME"] = str(home)
        os.environ.pop("BUILD_HOME", None)
        return home, p

    def _osm_rows_loaded(self, osm_path):
        """add_osm 을 **실제로 호출**해 적재 행수를 센다 — 술어 3번의 실측치."""
        db = sqlite3.connect(self.tmp / f"probe{id(osm_path)}.sqlite")
        self.addCleanup(db.close)
        db.executescript(GG.SCHEMA)
        st = new_state()
        err = io.StringIO()
        old, sys.stderr = sys.stderr, err
        try:
            GG.add_osm(db, osm_path, st)
        finally:
            sys.stderr = old
        return st["pid"]

    def test_literal_tilde_osm_splits_three_predicates_until_normalized(self):
        home, real = self._fake_home_with_osm()
        src = self.tmp / "src"
        src.mkdir()
        for kind in ("build", "jibun"):
            (src / f"match_{kind}_sejong.txt").write_text("", encoding="utf-8")
        eff = ["sejong"]
        args = fake_args(osm="~/geocode-build/osm.sqlite", out=str(self.tmp / "out.sqlite"))
        self.assertTrue(args.osm_explicit)
        self.assertFalse(GG.targets_canonical(args.out))     # 정본 조준이 아니다(승격은 explicit 로만)

        # ── 하중: 정규화 전에는 세 지점이 갈린다 ─────────────────────────────
        g2 = GG.gate_inputs(src, eff, args)
        g3, _g4 = GG.gate_rowcounts(1, 1, eff, args)
        self.assertEqual(g2.verdict, "PASS")                 # gate_inputs: "osm 있다"
        self.assertNotIn("osm", g3.actual)                   # gate_rowcounts: "없다" → 기준선이 낮아진다
        self.assertEqual(self._osm_rows_loaded(args.osm), 0)  # add_osm: "없다" → 통째로 건너뛴다

        # ── 조치: 1회 정규화 후 세 지점이 같은 답을 낸다 ─────────────────────
        GG.normalize_arg_paths(args)
        self.assertEqual(pathlib.Path(args.osm), real)
        g2b = GG.gate_inputs(src, eff, args)
        g3b, _g4b = GG.gate_rowcounts(1, 1, eff, args)
        self.assertEqual(g2b.verdict, "PASS")
        self.assertIn(f"osm {GG.BASELINE_OSM:,}", g3b.actual)  # 기준선이 더는 조용히 낮아지지 않는다
        self.assertEqual(self._osm_rows_loaded(args.osm), 2)

    def test_literal_tilde_build_home_no_longer_writes_beside_cwd(self):
        """`BUILD_HOME='~/geocode-build'` — 기본 `--out` 조립이 상대경로를 만든다.

        아래 조립식은 main() `:859` 의 기본값 식을 그대로 옮긴 것이다. 정규화 전에는
        `~/geocode-build/geocode.sqlite` 라는 **상대경로**가 나와 `out.parent.mkdir()` 이
        작업 디렉터리 아래 `./~/geocode-build` 를 만든다 — 게이트가 정본이라고 판정한 곳과
        **다른 자리에** 쓴다.
        """
        home = self.tmp / "home"
        (home / "geocode-build").mkdir(parents=True)
        os.environ["HOME"] = str(home)
        os.environ["BUILD_HOME"] = "~/geocode-build"
        assembled = os.path.join(os.environ.get("BUILD_HOME")
                                 or os.path.expanduser("~/geocode-build"), "geocode.sqlite")
        self.assertFalse(os.path.isabs(assembled))           # 하중: 결함 전제 재현
        args = fake_args(out=assembled)
        # G0 는 정규화 전에도 이것을 정본으로 본다(_abspath 가 expanduser 한다) — 이 조치로 약화되지 않는다
        self.assertTrue(GG.targets_canonical(args.out))
        GG.normalize_arg_paths(args)
        self.assertTrue(os.path.isabs(args.out))
        self.assertEqual(pathlib.Path(args.out), home / "geocode-build" / "geocode.sqlite")
        self.assertTrue(GG.targets_canonical(args.out))      # 정규화 후에도 여전히 정본이다

    def test_normalization_leaves_ordinary_paths_alone(self):
        """과잉 조치 방지 — 이미 정규인 경로와 빈 문자열은 건드리지 않는다.

        빈 문자열을 `Path("").expanduser()` 로 통과시키면 `.` 이 되어, 없던 경로가
        '있는 디렉터리'로 둔갑한다(gate_rowcounts 가 osm 기준선을 얹어 버린다).
        """
        p = str(self.tmp / "out.sqlite")
        q = str(self.tmp / "osm.sqlite")
        args = fake_args(out=p, osm=q)
        GG.normalize_arg_paths(args)
        self.assertEqual((args.out, args.osm), (p, q))
        a2 = fake_args(out=p, osm="")
        GG.normalize_arg_paths(a2)
        self.assertEqual(a2.osm, "")

    def test_main_actually_calls_the_normalizer_end_to_end(self):
        """**배선 시험** — 정규화 함수가 있는 것과 `main()` 이 그걸 부르는 것은 다르다.

        이 시험은 자체 mutation 에서 나왔다. `main()` 의 호출 줄 `normalize_arg_paths(args)`
        를 지워도 위 단위시험 3건이 **전부 살아남았다**(m5 SURVIVED). 함수만 시험하면
        정규화가 실제 실행 경로에 꽂혀 있는지는 아무도 보증하지 않는다 —
        N-2("층은 있는데 그 층을 지키는 시험이 없다")와 정확히 같은 부류의 공백이다.

        그래서 스크립트를 **하위 프로세스로 실제 실행**해 세 소비 지점의 결과를 관측한다:
          (1) 시작 배너의 `osm=` — 정규화가 일어났는가
          (2) G3 기준선에 `osm 594,704` 가 얹혔는가 — `gate_rowcounts` 술어
          (3) 산출 tmp 의 `source='osm'` 행수 — `add_osm` 술어가 실제로 적재했는가

        **하중은 (3) 에 있다.** 배너 문자열만 보면 정규화의 '흔적'만 보는 것이고,
        행수는 osm 이 실제로 들어갔음을 재현 가능하게 증명한다. 정규화가 없으면
        `add_osm` 은 리터럴 틸데 경로를 못 찾아 조용히 건너뛰고 0 행이 된다.

        안전(지시서 §3): `--only sejong` 부분 빌드 · `--out` 은 `/private/tmp` 아래 ·
        `HOME` 을 임시 디렉터리로 돌려 정본 자산에 닿지 않는다. 게이트가 전부 막아
        `tmp.replace(out)` 까지 가지 않으므로 산출물도 남지 않는다.
        """
        home, real = self._fake_home_with_osm()
        src = self.tmp / "src"
        src.mkdir()
        for kind in ("build", "jibun"):
            (src / f"match_{kind}_sejong.txt").write_text("", encoding="utf-8")

        # --out 은 반드시 /private/tmp 아래(지시서 §3). 임시 디렉터리도 거기에 판다.
        od = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(od.cleanup)
        out = pathlib.Path(od.name) / "B.sqlite"
        self.assertTrue(str(out).startswith("/private/tmp/"))

        literal = "~/geocode-build/osm.sqlite"
        self.assertTrue(literal.startswith("~/"))         # 하중: 결함 전제(리터럴 틸데) 재현

        env = dict(os.environ)
        env["HOME"] = str(home)
        env.pop("BUILD_HOME", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        r = subprocess.run(
            [sys.executable, str(HERE / "09-gen-geocode.py"),
             "--src", str(src), "--only", "sejong",
             "--out", str(out), "--osm", literal],
            capture_output=True, text=True, timeout=180, env=env, cwd=str(self.tmp))
        err = r.stderr
        tail = err[-3000:]

        # (1) 배너 — main() 이 확장한 절대경로를 들고 있다
        self.assertIn(f"osm={real}", err, tail)
        self.assertNotIn("osm=~/", err, tail)
        # (2) G3 기준선 — gate_rowcounts 가 osm 을 '있다'로 봤다
        self.assertIn(f"osm {GG.BASELINE_OSM:,}", err, tail)
        # 교체는 게이트가 막았다 — out 은 만들어지지 않는다
        self.assertIn("산출물 교체 중단", err, tail)
        self.assertFalse(out.exists())

        # (3) 하중 — add_osm 이 실제로 2행을 적재했다(정규화 없으면 0행)
        tmpdb = out.with_suffix(".sqlite.tmp")
        self.assertTrue(tmpdb.exists(), f"조사용 tmp 산출물이 없다: {tail}")
        db = sqlite3.connect(f"file:{tmpdb}?mode=ro", uri=True)
        self.addCleanup(db.close)
        self.assertEqual(
            db.execute("SELECT count(*) FROM places WHERE source='osm'").fetchone()[0], 2)

    def test_empty_out_is_preserved_by_the_guard(self):
        """`if args.out:` 가드에 하중을 지운다 (3차 검수 M-B).

        검수 실증: `:580` 을 `if True:` 로 바꿔도 55개 시험이 하나도 안 죽었다.
        같은 모양의 osm 측 가드(`:578`)는 죽는데 out 측만 무보호였다 —
        `normalize_arg_paths` 의 docstring 이 **양쪽 모두에 하중이 있는 것처럼**
        서술하고 있었으므로 서술과 시험이 어긋난 상태였다.

        **실동작 위험은 0 이다.** 실측: `_abspath("")` 와 `_abspath(".")` 는 둘 다
        cwd 로 수렴하고, `pathlib.Path("")` 는 애초에 `PosixPath(".")` 다. 그래서
        `--out ''` 은 가드 유무와 무관하게 같게 동작한다. **이 시험의 하중은
        위험 차단이 아니라 계약 보존**이다 — 가드를 남겨 둘 거라면 그 가드가
        하는 일을 시험이 지켜야 하고, 지키지 않을 거면 가드를 지워야 한다.

        osm 측은 사정이 다르다: `""` → `"."` 이면 `Path(".").exists()` 가 True 라
        `gate_rowcounts` 가 없는 osm 의 기준선 594,704 를 얹는다. 그쪽 하중은
        `test_normalization_leaves_ordinary_paths_alone` 이 이미 지고 있다.
        """
        a = fake_args(out="", osm=str(self.tmp / "osm.sqlite"))
        GG.normalize_arg_paths(a)
        self.assertEqual(a.out, "", "가드가 빈 --out 을 '.' 로 바꿨다 — :580 을 확인하라")
        # 양쪽 동시에 빈 경우도 보존한다(가드 2개가 서로 독립임을 고정)
        b = fake_args(out="", osm="")
        GG.normalize_arg_paths(b)
        self.assertEqual((b.out, b.osm), ("", ""))

    def test_normalization_runs_after_the_osm_default_is_assembled(self):
        """정규화 호출의 **위치**를 고정한다 (3차 검수 M-C).

        검수 실증: `main()` 의 `normalize_arg_paths(args)` 를 `--osm` 기본값 조립
        **앞**으로 옮겨도 55개 시험이 하나도 안 죽었다. 현행 코드가 정상인 것은
        기본값이 `os.path.expanduser("~/geocode-build")` 로 이미 확장돼 오기 때문이지,
        순서가 지켜져서가 아니다 — 즉 **우연히** 맞다.

        그런데 `BUILD_HOME` 은 확장을 거치지 않는다. 리터럴 틸데로 주면 기본값이
        `~/geocode-build/osm.sqlite` 로 조립되고, 그때부터 순서가 결정적이 된다:
          - 조립 **뒤**에 정규화(현행) → `args.osm` 은 확장된 절대경로
          - 조립 **앞**에 정규화(변형) → 그 시점의 `args.osm` 은 None 이라 가드가
            건너뛰고, 리터럴 틸데가 그대로 세 소비 지점까지 흘러간다 = N-1 재발

        그래서 `BUILD_HOME` 을 리터럴 틸데로 준 채 `main()` 을 실제로 돌려 관측한다.
        `--osm` 은 **주지 않는다** — 기본값 조립 경로를 타야 순서가 시험된다.

        안전(지시서 §3): `--only sejong` 부분 빌드 · `--out` 은 `/private/tmp` 아래 ·
        `HOME` 과 `BUILD_HOME` 을 임시 디렉터리로 돌려 정본 자산에 닿지 않는다.
        """
        home, real = self._fake_home_with_osm()
        src = self.tmp / "src"
        src.mkdir()
        for kind in ("build", "jibun"):
            (src / f"match_{kind}_sejong.txt").write_text("", encoding="utf-8")

        od = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(od.cleanup)
        out = pathlib.Path(od.name) / "B.sqlite"
        self.assertTrue(str(out).startswith("/private/tmp/"))

        literal_home = "~/geocode-build"
        self.assertTrue(literal_home.startswith("~/"))   # 하중: 결함 전제(리터럴 틸데) 재현

        env = dict(os.environ)
        env["HOME"] = str(home)
        env["BUILD_HOME"] = literal_home
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        r = subprocess.run(
            [sys.executable, str(HERE / "09-gen-geocode.py"),
             "--src", str(src), "--only", "sejong", "--out", str(out)],   # --osm 없음
            capture_output=True, text=True, timeout=180, env=env, cwd=str(self.tmp))
        err = r.stderr
        tail = err[-3000:]

        # 하중 — 배너의 osm 이 확장된 절대경로다. 호출이 조립 앞으로 가면 여기서 죽는다.
        self.assertIn(f"osm={real}", err, tail)
        self.assertNotIn("osm=~", err, tail)
        # 두 번째 소비 지점 — gate_rowcounts 도 같은 경로를 '있다'로 봤다
        self.assertIn(f"osm {GG.BASELINE_OSM:,}", err, tail)
        self.assertIn("산출물 교체 중단", err, tail)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
