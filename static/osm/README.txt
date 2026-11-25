InTrAvia hidden routing graph / OSM data

The application loads an OSM-derived GeoJSON road network as a hidden routing graph
(not as a visible Leaflet layer) when one of these files is present:
  - static/osm/svo_roads.geojson
  - static/osm/vdnh_roads.geojson

Accepted geometry: LineString or MultiLineString in WGS84 GeoJSON.
Useful OSM properties: highway, maxspeed (or maxspeed_kmh), and optional spd_kmh.
Road class is used as a mild route preference (primary/secondary/tertiary roads are
preferred where sensible); ETA is calculated from per-segment speed limits.

No OSM extract was included in the supplied project archive. Until an OSM GeoJSON
extract is added, the router uses the bundled static/roads.geojson and
static/vdnh/roads.geojson datasets as offline fallback networks. The road graph is
used only for route calculations and is never drawn on the visible map.

If redistributing OSM-derived data, include OpenStreetMap attribution and comply
with the ODbL: https://www.openstreetmap.org/copyright
