-- MVT 짧은 반복 쿼리: JIT 컴파일 오버헤드 제거 (apply-schema.sh 가 schema/*.sql 번호순 적용)
ALTER ROLE cuvia SET jit = off;
