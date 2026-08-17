-- ============================================================================
-- T026 — 인천 자치구 개편 대응표 **데이터 스냅샷**
--
--   [근거: VWorld dsId=30505 OLD_LAWDCD]
--   성격    : **열람·리뷰용 데이터 덤프.** 정본(正本) 생성은
--             scripts/postgis/build_incheon_remap_from_old_lawdcd.sql (자동 도출 79행) +
--             scripts/postgis/lawd_sgg_remap_manual_fix.sql (수기 보정 1행) 이며
--             이 파일은 그 산출 결과를 그대로 떠낸 것이다.
--             **두 파일과 어긋나면 위 두 파일이 우선한다.**
--   목적    : 리뷰어가 **DB 를 갖추고 스크립트를 돌리기 전에도** 대응 내용을
--             그대로 읽을 수 있게 한다(T018 lawd_sido_remap_data.sql 선례).
--   추출    : 2026-08-18, 로컬 PostGIS (localhost:5433, DB cuvia).
--             SELECT 만 사용했고 DB 를 일절 변경하지 않았다.
--   행수    : lawd_sgg_remap 80행
--             = 자동 도출 79 (src='vworld-30505:old_lawdcd')
--             + 수기 보정 1 (src='manual:vworld-30505-missing-row')
--
--   교차표 (수기 보정 1행 포함 — 계획서 §1-4 의 자동 도출 표는 28110→28125 가 43쌍):
--   28110 중구       → 28125 제물포구        44쌍
--   28110 중구       → 28155 영종구          8쌍
--   28140 동구       → 28125 제물포구         7쌍
--   28260 서구       → 28275 서해구         11쌍
--   28260 서구       → 28290 검단구         10쌍
--
--   실행(선택): 이 파일 단독으로도 적재 가능하다. 멱등하게 동작하도록
--               CREATE TABLE IF NOT EXISTS + ON CONFLICT DO NOTHING 을 쓴다.
--                 psql -v ON_ERROR_STOP=1 -f scripts/postgis/lawd_sgg_remap_data.sql
--               이미 정본 스크립트로 만든 표가 있으면 아무것도 바꾸지 않는다.
--
--   주의    : 이 표는 **응답 경계 치환 전용**이다. 안 A 에서 DB 내부값은 28110/28140/
--             28260 으로 남는다. DB 를 직접 조회하면 API 응답과 다른 코드가 보인다 —
--             결함이 아니라 안 A 의 정의다(docs/incheon-sgg-remap.md).
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS lawd_sgg_remap (
  old_emd8   char(8) PRIMARY KEY,   -- 옛 읍면동 8자리 (28110xxx / 28140xxx / 28260xxx)
  new_emd8   char(8) NOT NULL,      -- 신 읍면동 8자리 (28125xxx / 28155xxx / 28275xxx / 28290xxx)
  old_sgg_nm text    NOT NULL,      -- 옛 시군구명 (중구 / 동구 / 서구)
  new_sgg_nm text    NOT NULL,      -- 신 시군구명 (제물포구 / 영종구 / 서해구 / 검단구)
  old_emd_nm text    NOT NULL,      -- 옛 읍면동명
  new_emd_nm text    NOT NULL,      -- 신 읍면동명
  n_rows     bigint  NOT NULL,      -- 해당 옛 8자리를 쓰는 address 행수(리뷰용 라벨)
  src        text    NOT NULL       -- 도출 근거
);

-- 입력 좁힘(§4-4 ⑦ 1단)이 new_sgg_nm 을 등호 대조 키로 쓴다 — 정본 스크립트와 동일하게 건다.
CREATE INDEX IF NOT EXISTS lawd_sgg_remap_new_sgg_nm_idx ON lawd_sgg_remap (new_sgg_nm);

-- ── lawd_sgg_remap (80행) ──────────────────────────────────────────────
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110101','28125108','중구','제물포구','중앙동1가','중앙동1가',43,'manual:vworld-30505-missing-row') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110102','28125109','중구','제물포구','중앙동2가','중앙동2가',48,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110103','28125110','중구','제물포구','중앙동3가','중앙동3가',79,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110104','28125111','중구','제물포구','중앙동4가','중앙동4가',67,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110105','28125112','중구','제물포구','해안동1가','해안동1가',13,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110106','28125113','중구','제물포구','해안동2가','해안동2가',44,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110107','28125114','중구','제물포구','해안동3가','해안동3가',10,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110108','28125115','중구','제물포구','해안동4가','해안동4가',6,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110109','28125116','중구','제물포구','관동1가','관동1가',72,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110110','28125117','중구','제물포구','관동2가','관동2가',54,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110111','28125118','중구','제물포구','관동3가','관동3가',47,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110112','28125119','중구','제물포구','항동1가','항동1가',52,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110113','28125120','중구','제물포구','항동2가','항동2가',2,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110114','28125121','중구','제물포구','항동3가','항동3가',2,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110115','28125122','중구','제물포구','항동4가','항동4가',6,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110116','28125123','중구','제물포구','항동5가','항동5가',9,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110117','28125124','중구','제물포구','항동6가','항동6가',9,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110118','28125125','중구','제물포구','항동7가','항동7가',1465,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110119','28125126','중구','제물포구','송학동1가','송학동1가',38,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110120','28125127','중구','제물포구','송학동2가','송학동2가',86,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110121','28125128','중구','제물포구','송학동3가','송학동3가',170,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110122','28125129','중구','제물포구','사동','사동',126,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110123','28125130','중구','제물포구','신생동','신생동',201,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110124','28125131','중구','제물포구','신포동','신포동',263,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110125','28125132','중구','제물포구','답동','답동',443,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110126','28125133','중구','제물포구','신흥동1가','신흥동1가',384,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110127','28125134','중구','제물포구','신흥동2가','신흥동2가',191,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110128','28125135','중구','제물포구','신흥동3가','신흥동3가',1155,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110129','28125136','중구','제물포구','선화동','선화동',237,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110130','28125137','중구','제물포구','유동','유동',245,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110131','28125138','중구','제물포구','율목동','율목동',399,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110132','28125139','중구','제물포구','도원동','도원동',712,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110133','28125140','중구','제물포구','내동','내동',423,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110134','28125141','중구','제물포구','경동','경동',465,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110135','28125142','중구','제물포구','용동','용동',175,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110136','28125143','중구','제물포구','인현동','인현동',306,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110137','28125144','중구','제물포구','전동','전동',460,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110138','28125145','중구','제물포구','북성동1가','북성동1가',1545,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110139','28125146','중구','제물포구','북성동2가','북성동2가',275,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110140','28125147','중구','제물포구','북성동3가','북성동3가',162,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110141','28125148','중구','제물포구','선린동','선린동',130,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110142','28125149','중구','제물포구','송월동1가','송월동1가',546,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110143','28125150','중구','제물포구','송월동2가','송월동2가',105,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110144','28125151','중구','제물포구','송월동3가','송월동3가',339,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110145','28155101','중구','영종구','중산동','중산동',1672,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110146','28155102','중구','영종구','운남동','운남동',1589,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110147','28155103','중구','영종구','운서동','운서동',2327,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110148','28155104','중구','영종구','운북동','운북동',1704,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110149','28155105','중구','영종구','을왕동','을왕동',1300,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110150','28155106','중구','영종구','남북동','남북동',649,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110151','28155107','중구','영종구','덕교동','덕교동',560,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28110152','28155108','중구','영종구','무의동','무의동',749,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28140101','28125101','동구','제물포구','만석동','만석동',1136,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28140102','28125102','동구','제물포구','화수동','화수동',1278,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28140103','28125103','동구','제물포구','송현동','송현동',1446,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28140104','28125104','동구','제물포구','화평동','화평동',599,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28140105','28125105','동구','제물포구','창영동','창영동',227,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28140106','28125106','동구','제물포구','금곡동','금곡동',669,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28140107','28125107','동구','제물포구','송림동','송림동',3094,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260101','28290101','서구','검단구','백석동','백석동',646,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260102','28290102','서구','검단구','시천동','시천동',243,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260103','28275101','서구','서해구','검암동','검암동',1414,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260104','28275102','서구','서해구','경서동','경서동',1524,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260105','28275103','서구','서해구','공촌동','공촌동',446,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260106','28275104','서구','서해구','연희동','연희동',987,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260107','28275105','서구','서해구','심곡동','심곡동',1017,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260108','28275106','서구','서해구','가정동','가정동',1843,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260109','28275107','서구','서해구','신현동','신현동',1477,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260110','28275108','서구','서해구','석남동','석남동',5927,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260111','28275109','서구','서해구','원창동','원창동',1541,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260112','28275110','서구','서해구','가좌동','가좌동',6355,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260113','28290103','서구','검단구','마전동','마전동',1777,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260114','28290104','서구','검단구','당하동','당하동',1270,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260115','28290105','서구','검단구','원당동','원당동',859,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260117','28290106','서구','검단구','대곡동','대곡동',1237,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260118','28290107','서구','검단구','금곡동','금곡동',1763,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260119','28290108','서구','검단구','오류동','오류동',3807,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260120','28290109','서구','검단구','왕길동','왕길동',2248,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260121','28290110','서구','검단구','불로동','불로동',696,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;
INSERT INTO lawd_sgg_remap (old_emd8,new_emd8,old_sgg_nm,new_sgg_nm,old_emd_nm,new_emd_nm,n_rows,src) VALUES ('28260122','28275111','서구','서해구','청라동','청라동',2430,'vworld-30505:old_lawdcd') ON CONFLICT DO NOTHING;

COMMIT;

-- ── 적재 후 자가검증 (기대: 80) ────────────────────────────────────────────
--   SELECT count(*) FROM lawd_sgg_remap;                          -->  80
--   SELECT src, count(*) FROM lawd_sgg_remap GROUP BY 1 ORDER BY 1;
--        -->  manual:vworld-30505-missing-row 1 / vworld-30505:old_lawdcd 79
--   SELECT count(*) FROM (SELECT old_emd8 FROM lawd_sgg_remap
--                          GROUP BY 1 HAVING count(*)>1) t;        -->  0
