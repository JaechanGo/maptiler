--
-- PostgreSQL database dump
--

-- Dumped from database version 17.5 (Debian 17.5-1.pgdg110+1)
-- Dumped by pg_dump version 17.5 (Debian 17.5-1.pgdg110+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: address; Type: TABLE; Schema: public; Owner: cuvia
--

CREATE TABLE public.address (
    id bigint NOT NULL,
    kind text,
    name text,
    subtype text,
    sido text,
    sigungu text,
    emd text,
    road text,
    road_norm text,
    main_no integer,
    sub_no integer,
    bld text,
    postal text,
    haeng_dong text,
    bd_mgt_sn text,
    bcode text,
    hcode text,
    phone text,
    opened text,
    jibun text,
    cat1 text,
    cat2 text,
    source text,
    is_primary smallint,
    geom public.geometry(Point,4326),
    search_text text GENERATED ALWAYS AS (TRIM(BOTH FROM ((((COALESCE(name, ''::text) || ' '::text) || COALESCE(road, ''::text)) || ' '::text) || COALESCE(jibun, ''::text)))) STORED,
    ri text
);


ALTER TABLE public.address OWNER TO cuvia;

--
-- Name: address_id_seq; Type: SEQUENCE; Schema: public; Owner: cuvia
--

CREATE SEQUENCE public.address_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.address_id_seq OWNER TO cuvia;

--
-- Name: address_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: cuvia
--

ALTER SEQUENCE public.address_id_seq OWNED BY public.address.id;


--
-- Name: poi; Type: TABLE; Schema: public; Owner: cuvia
--

CREATE TABLE public.poi (
    id bigint NOT NULL,
    kind text,
    name text,
    subtype text,
    cat1 text,
    cat2 text,
    source text,
    is_primary smallint,
    phone text,
    geom public.geometry(Point,4326) NOT NULL,
    tier_minzoom smallint
);


ALTER TABLE public.poi OWNER TO cuvia;

--
-- Name: poi_id_seq; Type: SEQUENCE; Schema: public; Owner: cuvia
--

CREATE SEQUENCE public.poi_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.poi_id_seq OWNER TO cuvia;

--
-- Name: poi_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: cuvia
--

ALTER SEQUENCE public.poi_id_seq OWNED BY public.poi.id;


--
-- Name: address id; Type: DEFAULT; Schema: public; Owner: cuvia
--

ALTER TABLE ONLY public.address ALTER COLUMN id SET DEFAULT nextval('public.address_id_seq'::regclass);


--
-- Name: poi id; Type: DEFAULT; Schema: public; Owner: cuvia
--

ALTER TABLE ONLY public.poi ALTER COLUMN id SET DEFAULT nextval('public.poi_id_seq'::regclass);


--
-- Name: address address_pkey; Type: CONSTRAINT; Schema: public; Owner: cuvia
--

ALTER TABLE ONLY public.address
    ADD CONSTRAINT address_pkey PRIMARY KEY (id);


--
-- Name: poi poi_pkey; Type: CONSTRAINT; Schema: public; Owner: cuvia
--

ALTER TABLE ONLY public.poi
    ADD CONSTRAINT poi_pkey PRIMARY KEY (id);


--
-- Name: address_addr_geom_gix; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX address_addr_geom_gix ON public.address USING gist (geom) WHERE (kind = 'addr'::text);


--
-- Name: address_bld_trgm; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX address_bld_trgm ON public.address USING gin (bld public.gin_trgm_ops);


--
-- Name: address_emd_trgm; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX address_emd_trgm ON public.address USING gin (emd public.gin_trgm_ops) WHERE (kind <> 'addr'::text);


--
-- Name: address_geom_gix; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX address_geom_gix ON public.address USING gist (geom);


--
-- Name: address_kind_idx; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX address_kind_idx ON public.address USING btree (kind);


--
-- Name: address_postal_idx; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX address_postal_idx ON public.address USING btree (postal) WHERE (kind = 'addr'::text);


--
-- Name: address_region_idx; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX address_region_idx ON public.address USING btree (sigungu, emd);


--
-- Name: address_road_addr_idx; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX address_road_addr_idx ON public.address USING btree (road_norm, main_no, sub_no) WHERE (kind = 'addr'::text);


--
-- Name: address_search_trgm; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX address_search_trgm ON public.address USING gin (search_text public.gin_trgm_ops);


--
-- Name: address_sido_trgm; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX address_sido_trgm ON public.address USING gin (sido public.gin_trgm_ops) WHERE (kind <> 'addr'::text);


--
-- Name: address_sigungu_trgm; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX address_sigungu_trgm ON public.address USING gin (sigungu public.gin_trgm_ops) WHERE (kind <> 'addr'::text);


--
-- Name: address_source_idx; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX address_source_idx ON public.address USING btree (source);


--
-- Name: address_synth_pnu_idx; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX address_synth_pnu_idx ON public.address USING btree (((bcode || substr(bd_mgt_sn, 11, 9)))) WHERE (kind = 'addr'::text);


--
-- Name: poi_geom_gix; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX poi_geom_gix ON public.poi USING gist (geom);


--
-- Name: poi_kind_idx; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX poi_kind_idx ON public.poi USING btree (kind);


--
-- Name: poi_primary_idx; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX poi_primary_idx ON public.poi USING btree (is_primary);


--
-- Name: poi_tier_idx; Type: INDEX; Schema: public; Owner: cuvia
--

CREATE INDEX poi_tier_idx ON public.poi USING btree (tier_minzoom);


--
-- PostgreSQL database dump complete
--

