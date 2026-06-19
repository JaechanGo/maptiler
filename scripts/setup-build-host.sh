#!/usr/bin/env bash
# 빌드 호스트 준비 — 내비DB(.7z) 추출용 p7zip 설치 + 빌드 툴체인 점검.
# OS/패키지매니저 자동감지: Ubuntu·Debian(apt) / RHEL·Rocky·CentOS·BlueOnyx(dnf|yum+EPEL) / macOS(brew).
#   ./scripts/setup-build-host.sh
# (폐쇄망 서빙 서버는 불필요 — 빌드는 인터넷 빌드 호스트에서만 7z가 필요)
set -uo pipefail
have(){ command -v "$1" >/dev/null 2>&1; }
SUDO=""; [ "$(id -u)" -ne 0 ] && have sudo && SUDO="sudo"

echo "── [1/2] p7zip(7z) ──────────────────────────────"
if have 7z || have 7za || have 7zr; then
  echo "✓ 이미 설치됨: $(command -v 7z 7za 7zr 2>/dev/null | head -1)"
elif have apt-get; then
  echo "→ apt: p7zip-full"; $SUDO apt-get update -qq && $SUDO apt-get install -y p7zip-full
elif have dnf; then
  echo "→ dnf: EPEL + p7zip"; $SUDO dnf install -y epel-release 2>/dev/null; $SUDO dnf install -y p7zip p7zip-plugins
elif have yum; then
  echo "→ yum: EPEL + p7zip"; $SUDO yum install -y epel-release 2>/dev/null; $SUDO yum install -y p7zip p7zip-plugins
elif have brew; then
  echo "→ brew: p7zip"; brew install p7zip
else
  echo "✗ 패키지 매니저(apt/dnf/yum/brew) 미발견 — p7zip 수동 설치 필요"
fi
if have 7z || have 7za || have 7zr; then echo "✓ 7z 사용 가능 → 내비DB .7z 업로드 시 빌드때 자동 추출"
else echo "✗ 7z 여전히 없음 — 위 메시지 확인(권한/네트워크) 또는 .txt 직접배치"; fi

echo
echo "── [2/2] 빌드 툴체인 점검(없으면 안내) ───────────"
for t in python3 java ogr2ogr gdaltransform tippecanoe tile-join; do
  if have "$t"; then echo "  ✓ $t"; else echo "  ✗ $t (없음 — 해당 빌드 단계에 필요)"; fi
done
echo
echo "툴체인 설치 예(필요 시):"
echo "  Ubuntu : $SUDO apt-get install -y gdal-bin default-jre   # tippecanoe/tile-join 은 소스 빌드"
echo "  RHEL   : $SUDO dnf install -y gdal java-17-openjdk        # tippecanoe 는 소스 빌드"
echo "  macOS  : brew install gdal tippecanoe openjdk"
