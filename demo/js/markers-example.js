// 마커는 본 서비스가 아니라 소비 프론트의 책임이다.
// 아래는 "우리 지도 위에 마커가 얹히는지"를 확인하는 예시 코드.
(function () {
  const map = window.cuviaMap;

  // 소량: DOM 마커 — 좌표 순서는 [경도, 위도]!
  new maplibregl.Marker({ color: '#e85c2a' })
    .setLngLat([126.9779, 37.5663])
    .setPopup(new maplibregl.Popup().setHTML('<b>서울시청</b>'))
    .addTo(map);

  // 대량: GeoJSON 소스 + 클러스터링 (수천~수만 개 권장 방식)
  map.on('load', () => {
    const pts = {
      type: 'FeatureCollection',
      features: Array.from({ length: 500 }, (_, i) => ({
        type: 'Feature',
        geometry: { type: 'Point',
          coordinates: [126.9 + Math.random() * 0.2, 37.45 + Math.random() * 0.15] },
        properties: { id: i },
      })),
    };
    map.addSource('demo-points', { type: 'geojson', data: pts, cluster: true, clusterRadius: 40 });
    map.addLayer({
      id: 'demo-clusters', type: 'circle', source: 'demo-points',
      paint: {
        'circle-color': '#4d7cfe', 'circle-opacity': 0.8,
        'circle-radius': ['case', ['has', 'point_count'],
          ['+', 10, ['*', 2, ['sqrt', ['get', 'point_count']]]], 5],
      },
    });
    map.addLayer({
      id: 'demo-cluster-count', type: 'symbol', source: 'demo-points',
      filter: ['has', 'point_count'],
      layout: {
        'text-field': ['to-string', ['get', 'point_count']],
        'text-font': ['KlokanTech Noto Sans Regular'], 'text-size': 11,
      },
      paint: { 'text-color': '#ffffff' },
    });
  });
})();
