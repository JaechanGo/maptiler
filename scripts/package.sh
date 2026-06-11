#!/usr/bin/env bash
# [온라인 단계] 폐쇄망 반입용 번들 생성: Docker 이미지 tar + 산출물 tgz
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
mkdir -p "$DIST"

echo "[1/3] 스타일 조립(최신화)"
"$ROOT/scripts/build-style.sh"

echo "[2/3] Docker 이미지 (linux/amd64 강제 — 폐쇄망 x86_64 용)"
# compose 파일에 고정된 태그를 그대로 사용해 드리프트를 방지한다.
# ※ ROOT 에 공백이 포함될 수 있으므로 while read 로 라인 단위 파싱 (bash 3.2 호환)
TAGS=()
while IFS= read -r _tag; do
  TAGS+=("$_tag")
done < <(grep -E '^\s+image:' "$ROOT/server/docker-compose.yml" | awk '{print $2}')

# Docker Desktop containerd 이미지 스토어에서는 multi-arch 인덱스 태그로 docker save를
# 하면 로컬에 없는 다른 아키텍처 레이어를 참조해 실패한다.
# 회피책: buildx imagetools inspect 로 linux/amd64 단일 플랫폼 매니페스트 다이제스트를
# 얻어 그것만 pull → save 한다 (에어갭 x86_64 서버에서 docker load 시 태그 지정 필요).
SAVE_REFS=()
for tag in "${TAGS[@]}"; do
  # buildx imagetools inspect 출력에서 linux/amd64 플랫폼 전 마지막 Name: 값을 추출
  # (Name: 라인이 Platform: 라인보다 앞에 나오므로 "마지막 Name 추적" 방식 사용)
  ref=$(docker buildx imagetools inspect "$tag" 2>/dev/null \
    | awk '/^  Name:/{name=$2} /^  Platform:.*linux\/amd64/{print name; exit}')
  if [ -z "$ref" ]; then
    echo "  경고: $tag 에서 linux/amd64 다이제스트를 찾지 못했습니다 — 태그 직접 사용"
    ref="$tag"
  fi
  echo "  pull $ref"
  docker pull "$ref"
  SAVE_REFS+=("$ref")
done

docker save -o "$DIST/images.tar" "${SAVE_REFS[@]}"
# 에어갭 서버에서 docker load 후 원래 버전 태그로 복원하는 명령 안내
echo "  ※ load 후 태그 복원 예시:"
for tag in "${TAGS[@]}"; do
  echo "      docker tag <LOADED_ID> $tag"
done

echo "[3/3] 산출물 번들"
tar -czf "$DIST/cuvia-map-bundle.tgz" -C "$ROOT" \
  tiles style demo vendor server scripts/deploy.sh docs/integration-guide.md
ls -lh "$DIST"
echo "반입 대상 2개: dist/images.tar, dist/cuvia-map-bundle.tgz"
