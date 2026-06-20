# CUVIA PostGIS 지오코더 이미지 — psycopg 베이크(폐쇄망 런타임 pip 불가 회피).
# package.sh 가 로컬 빌드(cuvia-geocode-pg:local) 후 docker save 로 번들 → deploy.sh docker load.
FROM python:3.12-slim
RUN pip install --no-cache-dir 'psycopg[binary]' psycopg_pool
COPY geocode-api-pg.py /app/geocode-api-pg.py
WORKDIR /app
CMD ["python3", "/app/geocode-api-pg.py"]
