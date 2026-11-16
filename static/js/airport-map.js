/* Движок карты аэродрома и демо-симуляция движения ВС.
 *
 * Рисует схему аэродрома из static/aircraft_paths.geojson (ВПП, РД, перроны),
 * ставит маркеры стоянок, строит граф рулёжных дорожек и гоняет по нему
 * самолёты: посадка → руление к стоянке → стоянка → вылет.
 *
 * window.AirportSim.init(map, options)
 *   options: {pathsUrl, stands, onStandClick, onStatus}
 * window.AirportSim.setParams({...}); start(); pause(); reset();
 */
(function () {
    'use strict';

    var DEFAULTS = {
        count: 4,          // сколько ВС одновременно
        speed: 25,         // км/ч по рулёжным дорожкам
        dwell: 12,         // сек на стоянке
        interval: 4,       // сек между выпусками
        timeScale: 20,     // ускорение демо-времени
        showLayout: true,  // показывать схему аэродрома
        showLabels: true
    };

    var EPS = 1e-9;
    var M_PER_DEG_LAT = 110540;
    var CLUSTER_M = 15;
    var STAND_LINK_MAX_M = 900;

    var sim = {
        map: null,
        ready: false,
        running: false,
        params: {},
        graph: null,
        layoutLayer: null,
        standLayer: null,
        aircraft: [],
        pool: [],
        grounded: {},
        poolIndex: 0,
        spawnIndex: 0,
        nextSpawnAt: 0,
        rafId: null,
        lastFrame: 0,
        options: {},
        stats: { segments: 0, nodes: 0, stands: 0 }
    };

    function now() { return (window.performance && performance.now) ? performance.now() : Date.now(); }

    // ---------- проекция и расстояния ----------
    function projector(refLat) {
        var kx = 111320 * Math.cos(refLat * Math.PI / 180);
        var ky = M_PER_DEG_LAT;
        var fn = function (lng, lat) { return {x: lng * kx, y: lat * ky}; };
        fn.kx = kx;
        fn.ky = ky;
        return fn;
    }

    function distanceMeters(a, b) {
        var dLat = (a.lat - b.lat) * M_PER_DEG_LAT;
        var dLng = (a.lng - b.lng) * 111320 * Math.cos((a.lat + b.lat) / 2 * Math.PI / 180);
        return Math.sqrt(dLat * dLat + dLng * dLng);
    }

    function bearingDeg(a, b) {
        var dLng = (b.lng - a.lng) * Math.cos((a.lat + b.lat) / 2 * Math.PI / 180);
        var dLat = b.lat - a.lat;
        return Math.atan2(dLng, dLat) * 180 / Math.PI;
    }

    // ---------- union-find ----------
    function makeDsu(n) {
        var parent = new Array(n), rank = new Array(n);
        for (var i = 0; i < n; i++) { parent[i] = i; rank[i] = 0; }
        function find(a) {
            while (parent[a] !== a) { parent[a] = parent[parent[a]]; a = parent[a]; }
            return a;
        }
        function union(a, b) {
            a = find(a); b = find(b);
            if (a === b) return false;
            if (rank[a] < rank[b]) { var t = a; a = b; b = t; }
            parent[b] = a;
            if (rank[a] === rank[b]) rank[a]++;
            return true;
        }
        return {find: find, union: union, parent: parent};
    }

    // ---------- граф ----------
    function buildGraph(data) {
        var project = projector(55.972);
        var segments = [];
        var vertices = [];
        var runwayFeatures = [];
        var minLat = 90, maxLat = -90, minLng = 180, maxLng = -180;

        for (var i = 0; i < data.features.length; i++) {
            var f = data.features[i];
            var g = f.geometry || {};
            if (g.type !== 'LineString') continue;
            var props = f.properties || {};
            var cat = props.category || 'taxiway';
            var coords = g.coordinates;
            if (!coords || coords.length < 2) continue;
            var idxs = [];
            for (var c = 0; c < coords.length; c++) {
                var p = project(coords[c][0], coords[c][1]);
                idxs.push(vertices.length);
                vertices.push({x: p.x, y: p.y, lng: coords[c][0], lat: coords[c][1], cat: cat});
                if (coords[c][1] < minLat) minLat = coords[c][1];
                if (coords[c][1] > maxLat) maxLat = coords[c][1];
                if (coords[c][0] < minLng) minLng = coords[c][0];
                if (coords[c][0] > maxLng) maxLng = coords[c][0];
            }
            segments.push({idxs: idxs, cat: cat});
            if (cat === 'runway') {
                runwayFeatures.push({ref: props.ref || '', coords: idxs.slice()});
            }
        }

        var dsu = makeDsu(vertices.length);
        // склеиваем близкие вершины (узлы рулёжек в OSM часто чуть расходятся)
        var cell = {};
        for (var v = 0; v < vertices.length; v++) {
            var key = Math.floor(vertices[v].x / CLUSTER_M) + ',' + Math.floor(vertices[v].y / CLUSTER_M);
            (cell[key] || (cell[key] = [])).push(v);
        }
        for (var v2 = 0; v2 < vertices.length; v2++) {
            var cx = Math.floor(vertices[v2].x / CLUSTER_M), cy = Math.floor(vertices[v2].y / CLUSTER_M);
            for (var dx = -1; dx <= 1; dx++) {
                for (var dy = -1; dy <= 1; dy++) {
                    var bucket = cell[(cx + dx) + ',' + (cy + dy)];
                    if (!bucket) continue;
                    for (var b = 0; b < bucket.length; b++) {
                        var w = bucket[b];
                        if (w <= v2) continue;
                        var ddx = vertices[v2].x - vertices[w].x, ddy = vertices[v2].y - vertices[w].y;
                        if (ddx * ddx + ddy * ddy <= CLUSTER_M * CLUSTER_M) dsu.union(v2, w);
                    }
                }
            }
        }
        // узел на каждый корень кластера
        var roots = {}, nodes = [];
        for (var v3 = 0; v3 < vertices.length; v3++) {
            var r = dsu.find(v3);
            if (!roots.hasOwnProperty(r)) {
                roots[r] = nodes.length;
                nodes.push({lng: vertices[v3].lng, lat: vertices[v3].lat, edges: [], main: false});
            }
        }
        // центр кластера — среднее (иначе самолёт дёргается на стыках)
        var sums = {};
        for (var v4 = 0; v4 < vertices.length; v4++) {
            var rr = dsu.find(v4), id = roots[rr];
            if (!sums[id]) sums[id] = {x: 0, y: 0, n: 0};
            sums[id].x += vertices[v4].x; sums[id].y += vertices[v4].y; sums[id].n++;
        }
        for (var sid in sums) {
            if (!sums.hasOwnProperty(sid)) continue;
            nodes[sid].lng = sums[sid].x / sums[sid].n / project.kx;
            nodes[sid].lat = sums[sid].y / sums[sid].n / project.ky;
        }
        var nodeOfVertex = new Array(vertices.length);
        for (var v5 = 0; v5 < vertices.length; v5++) {
            nodeOfVertex[v5] = roots[dsu.find(v5)];
        }
        for (var rf = 0; rf < runwayFeatures.length; rf++) {
            runwayFeatures[rf].nodeIds = runwayFeatures[rf].coords.map(function (vi) {
                return nodeOfVertex[vi];
            });
        }

        // рёбра
        var edges = [];
        for (var s2 = 0; s2 < segments.length; s2++) {
            var ids2 = segments[s2].idxs;
            for (var k2 = 0; k2 < ids2.length - 1; k2++) {
                var a = roots[dsu.find(ids2[k2])], z = roots[dsu.find(ids2[k2 + 1])];
                if (a === z) continue;
                edges.push({a: a, b: z, w: distanceMeters(nodes[a], nodes[z]), cat: segments[s2].cat});
            }
        }

        // самая большая компонента — «настоящая» сеть
        var compSize = {};
        for (var e = 0; e < edges.length; e++) {
            compSize[edges[e].a] = (compSize[edges[e].a] || 0) + 1;
            compSize[edges[e].b] = (compSize[edges[e].b] || 0) + 1;
        }
        var mainRoot = -1, best = -1;
        var dsu2 = makeDsu(nodes.length);
        for (var e2 = 0; e2 < edges.length; e2++) dsu2.union(edges[e2].a, edges[e2].b);
        var sizes = {};
        for (var n = 0; n < nodes.length; n++) {
            var cr = dsu2.find(n);
            sizes[cr] = (sizes[cr] || 0) + 1;
            if (sizes[cr] > best) { best = sizes[cr]; mainRoot = cr; }
        }
        var mainNodes = [];
        for (var n2 = 0; n2 < nodes.length; n2++) {
            if (dsu2.find(n2) === mainRoot) { nodes[n2].main = true; mainNodes.push(n2); }
        }
        // на случай разрывов в OSM: соседние компоненты соединяем кратчайшим мостом
        if (mainNodes.length > 1) {
            for (var ci = 0; ci < nodes.length; ci++) {
                if (nodes[ci].main) continue;
                var nearest = -1, nd = Infinity;
                for (var m = 0; m < mainNodes.length; m++) {
                    var dd = distanceMeters(nodes[ci], nodes[mainNodes[m]]);
                    if (dd < nd) { nd = dd; nearest = mainNodes[m]; }
                }
                if (nearest >= 0 && nd < 60) {
                    edges.push({a: ci, b: nearest, w: nd, cat: 'bridge'});
                    nodes[ci].main = true;
                }
            }
            mainNodes = [];
            for (var n3 = 0; n3 < nodes.length; n3++) if (nodes[n3].main) mainNodes.push(n3);
        }

        for (var e3 = 0; e3 < edges.length; e3++) {
            var A = edges[e3].a, B = edges[e3].b, W = edges[e3].w;
            if (!nodes[A].main || !nodes[B].main) continue;
            nodes[A].edges.push({to: B, w: W});
            nodes[B].edges.push({to: A, w: W});
        }

        return {
            nodes: nodes,
            mainNodes: mainNodes,
            runwayFeatures: runwayFeatures,
            project: project,
            bounds: [[minLat, minLng], [maxLat, maxLng]]
        };
    }

    function nearestMainNode(graph, lng, lat) {
        var best = -1, dist = Infinity;
        for (var i = 0; i < graph.mainNodes.length; i++) {
            var id = graph.mainNodes[i];
            var dm = distanceMeters(graph.nodes[id], {lat: lat, lng: lng});
            if (dm < dist) { dist = dm; best = id; }
        }
        return {id: best, distance: dist};
    }

    // ---------- Дейкстра ----------
    function MinHeap() {
        this.a = [];
    }
    MinHeap.prototype.push = function (item, pri) {
        this.a.push({item: item, pri: pri});
        var i = this.a.length - 1;
        while (i > 0) {
            var p = (i - 1) >> 1;
            if (this.a[p].pri <= this.a[i].pri) break;
            var t = this.a[p]; this.a[p] = this.a[i]; this.a[i] = t; i = p;
        }
    };
    MinHeap.prototype.pop = function () {
        if (!this.a.length) return null;
        var top = this.a[0], last = this.a.pop();
        if (this.a.length) {
            this.a[0] = last;
            var i = 0;
            while (true) {
                var l = i * 2 + 1, r = l + 1, m = i;
                if (l < this.a.length && this.a[l].pri < this.a[m].pri) m = l;
                if (r < this.a.length && this.a[r].pri < this.a[m].pri) m = r;
                if (m === i) break;
                var t = this.a[i]; this.a[i] = this.a[m]; this.a[m] = t; i = m;
            }
        }
        return top;
    };
    MinHeap.prototype.size = function () { return this.a.length; };

    function shortestPath(graph, startId, endId) {
        if (startId === endId) return [startId];
        var dist = {}, prev = {}, heap = new MinHeap();
        dist[startId] = 0;
        heap.push(startId, 0);
        while (heap.size()) {
            var cur = heap.pop();
            if (!cur) break;
            var u = cur.item;
            if (u === endId) break;
            if (cur.pri > (dist[u] === undefined ? Infinity : dist[u])) continue;
            var edges = graph.nodes[u].edges;
            for (var i = 0; i < edges.length; i++) {
                var nx = edges[i].to, nd = cur.pri + edges[i].w;
                if (dist[nx] === undefined || nd < dist[nx]) {
                    dist[nx] = nd; prev[nx] = u;
                    heap.push(nx, nd);
                }
            }
        }
        if (dist[endId] === undefined) return null;
        var path = [endId], p = endId;
        while (p !== startId) { p = prev[p]; if (p === undefined) return null; path.push(p); }
        return path.reverse();
    }

    // ---------- точки маршрута ----------
    function pathToPoints(graph, nodeIds, extraEnd) {
        var pts = [];
        for (var i = 0; i < nodeIds.length; i++) {
            var nd = graph.nodes[nodeIds[i]];
            pts.push(L.latLng(nd.lat, nd.lng));
        }
        if (extraEnd) pts.push(L.latLng(extraEnd.lat, extraEnd.lng));
        // убираем дубли подряд
        var out = [];
        for (var j = 0; j < pts.length; j++) {
            if (!out.length || out[out.length - 1].distanceTo(pts[j]) > 0.5) out.push(pts[j]);
        }
        return out;
    }

    function cumulative(points) {
        var acc = [0], total = 0;
        for (var i = 1; i < points.length; i++) {
            total += points[i - 1].distanceTo(points[i]);
            acc.push(total);
        }
        return {acc: acc, total: total};
    }

    function pointAt(points, cum, meters) {
        if (!points.length) return {lat: 0, lng: 0, bearing: 0};
        if (meters <= 0) return {lat: points[0].lat, lng: points[0].lng};
        if (meters >= cum.total) {
            var last = points[points.length - 1];
            var before = points[points.length - 2] || last;
            return {lat: last.lat, lng: last.lng, bearing: bearingDeg(before, last)};
        }
        for (var i = 1; i < points.length; i++) {
            if (cum.acc[i] >= meters) {
                var seg = cum.acc[i] - cum.acc[i - 1] || EPS;
                var t = (meters - cum.acc[i - 1]) / seg;
                var a = points[i - 1], b = points[i];
                return {
                    lat: a.lat + (b.lat - a.lat) * t,
                    lng: a.lng + (b.lng - a.lng) * t,
                    bearing: bearingDeg(a, b)
                };
            }
        }
        return {lat: points[0].lat, lng: points[0].lng};
    }

    // ---------- слой схемы и стоянок ----------
    function addLayout(map, data) {
        var group = L.layerGroup();
        var styles = {
            runway: {color: '#334155', weight: 9, opacity: 0.55},
            taxiway: {color: '#f59e0b', weight: 2.5, opacity: 0.8},
            apron: {color: '#94a3b8', weight: 12, opacity: 0.25}
        };
        L.geoJSON(data, {
            filter: function (f) {
                var c = (f.properties || {}).category;
                return c === 'runway' || c === 'taxiway' || c === 'apron';
            },
            style: function (f) {
                var c = (f.properties || {}).category || 'taxiway';
                return styles[c] || styles.taxiway;
            },
            onEachFeature: function (f, layer) {
                var p = f.properties || {};
                if (p.ref || p.name) {
                    layer.bindTooltip((p.category || '') + ' ' + (p.ref || p.name || ''), {sticky: true});
                }
            }
        }).addTo(group);
        group.addTo(map);
        return group;
    }

    function addStandMarkers(map, stands, onStandClick) {
        var group = L.layerGroup();
        Object.keys(stands).forEach(function (ref) {
            var p = stands[ref];
            if (!p) return;
            var marker = L.marker([p.lat, p.lng], {
                icon: L.divIcon({
                    className: 'airport-stand-marker',
                    html: '<div class="airport-stand"><span>' + ref + '</span></div>',
                    iconSize: [22, 22], iconAnchor: [11, 11]
                }),
                keyboard: false
            });
            marker.bindTooltip('Стоянка ' + ref, {direction: 'top'});
            if (onStandClick) {
                marker.on('click', function () { onStandClick(ref); });
            }
            marker.addTo(group);
        });
        group.addTo(map);
        return group;
    }

    // ---------- самолёты ----------
    function aircraftLabel() {
        if (sim.pool.length) {
            var label = sim.pool[sim.poolIndex % sim.pool.length];
            sim.poolIndex++;
            return label;
        }
        return 'SU-' + (1000 + Math.floor(Math.random() * 8999));
    }

    function makePlaneMarker(aircraft) {
        var html =
            '<div class="sim-plane-wrap">' +
            '<div class="sim-plane-label">' + aircraft.label + '</div>' +
            '<div class="sim-plane-icon"><i class="fas fa-plane"></i></div>' +
            '</div>';
        var icon = L.divIcon({
            className: 'sim-plane-marker',
            html: html,
            iconSize: [28, 28], iconAnchor: [14, 14]
        });
        var marker = L.marker([aircraft.pos.lat, aircraft.pos.lng], {
            icon: icon, keyboard: false, zIndexOffset: 800
        }).addTo(sim.map);
        aircraft.marker = marker;
        updatePlaneVisual(aircraft);
    }

    function updatePlaneVisual(aircraft) {
        if (!aircraft.marker) return;
        aircraft.marker.setLatLng([aircraft.pos.lat, aircraft.pos.lng]);
        var el = aircraft.marker.getElement();
        if (!el) return;
        var icon = el.querySelector('.sim-plane-icon');
        if (icon) icon.style.transform = 'rotate(' + (aircraft.pos.bearing || 0) + 'deg)';
        var label = el.querySelector('.sim-plane-label');
        if (label) label.style.display = sim.params.showLabels ? 'block' : 'none';
        el.setAttribute('data-sim-plane', aircraft.label);
        el.setAttribute('data-phase', aircraft.phase);
        el.classList.toggle('at-stand', aircraft.phase === 'stand');
        el.classList.toggle('aog', !!aircraft.aog);
    }

    function chooseRunwayEnd() {
        var ends = sim.graph.runwayEnds;
        if (!ends || !ends.length) return null;
        return ends[sim.spawnIndex % ends.length];
    }

    function chooseStand() {
        var keys = sim.graph.standKeys;
        if (!keys || !keys.length) return null;
        var ref = keys[sim.spawnIndex % keys.length];
        return {ref: ref, nodeId: sim.graph.stands[ref].nodeId, lat: sim.graph.stands[ref].lat, lng: sim.graph.stands[ref].lng};
    }

    function spawnAircraft() {
        if (!sim.ready) return;
        var stand = chooseStand();
        if (!stand) return;
        var label = aircraftLabel();

        // AOG-борт не летает: ставим его на стоянку и оставляем красным
        if (sim.grounded[label]) {
            var grounded = {
                id: 'sim_aog_' + now(),
                label: label,
                standId: stand.ref,
                standNodeId: stand.nodeId,
                runwayEnd: null,
                phase: 'grounded',
                points: [L.latLng(stand.lat, stand.lng)],
                cum: {acc: [0], total: 0},
                distance: 0,
                waitLeft: 0,
                pos: {lat: stand.lat, lng: stand.lng, bearing: 0},
                aog: true
            };
            makePlaneMarker(grounded);
            sim.aircraft.push(grounded);
            sim.spawnIndex++;
            return;
        }

        var runwayEnd = chooseRunwayEnd();
        if (!runwayEnd) return;

        var pathIds = shortestPath(sim.graph, runwayEnd.nodeId, stand.nodeId);
        var points;
        if (pathIds && pathIds.length > 1) {
            points = pathToPoints(sim.graph, pathIds, stand);
        } else {
            points = [L.latLng(runwayEnd.lat, runwayEnd.lng), L.latLng(stand.lat, stand.lng)];
        }
        var cum = cumulative(points);
        var aircraft = {
            id: 'sim_' + now() + '_' + Math.floor(Math.random() * 1000),
            label: label,
            standId: stand.ref,
            runwayEnd: runwayEnd,
            phase: 'taxi_in',
            points: points,
            cum: cum,
            distance: 0,
            waitLeft: 0,
            pos: {lat: points[0].lat, lng: points[0].lng, bearing: bearingDeg(points[0], points[1] || points[0])},
            aog: false
        };
        makePlaneMarker(aircraft);
        sim.aircraft.push(aircraft);
        sim.spawnIndex++;
    }

    function beginTaxiOut(aircraft) {
        var end = aircraft.runwayEnd;
        var pathIds = shortestPath(sim.graph, aircraft.standNodeId, end.nodeId);
        var points = pathIds && pathIds.length > 1
            ? pathToPoints(sim.graph, pathIds)
            : [L.latLng(aircraft.pos.lat, aircraft.pos.lng), L.latLng(end.lat, end.lng)];
        aircraft.phase = 'taxi_out';
        aircraft.points = points;
        aircraft.cum = cumulative(points);
        aircraft.distance = 0;
    }

    function beginTakeoff(aircraft) {
        var end = aircraft.runwayEnd;
        var dir = end.direction || 0;
        var rad = dir * Math.PI / 180;
        // продолжаем по оси ВПП ~2.2 км и убираем борт
        var lat = end.lat + Math.cos(rad) * 0.02;
        var lng = end.lng + Math.sin(rad) * 0.02 / Math.cos(end.lat * Math.PI / 180);
        aircraft.phase = 'takeoff';
        aircraft.points = [L.latLng(aircraft.pos.lat, aircraft.pos.lng), L.latLng(lat, lng)];
        aircraft.cum = cumulative(aircraft.points);
        aircraft.distance = 0;
    }

    function removeAircraft(aircraft) {
        if (aircraft.marker) {
            try { sim.map.removeLayer(aircraft.marker); } catch (e) {}
        }
        var idx = sim.aircraft.indexOf(aircraft);
        if (idx >= 0) sim.aircraft.splice(idx, 1);
    }

    function tick(ts) {
        if (!sim.running) return;
        var dt = sim.lastFrame ? Math.min(0.2, (ts - sim.lastFrame) / 1000) : 0;
        sim.lastFrame = ts;

        var wallNow = Date.now() / 1000;
        if (sim.aircraft.length < sim.params.count && wallNow >= sim.nextSpawnAt) {
            spawnAircraft();
            sim.nextSpawnAt = wallNow + Math.max(1, sim.params.interval);
        }

        var taxiMps = sim.params.speed / 3.6 * sim.params.timeScale;
        for (var i = sim.aircraft.length - 1; i >= 0; i--) {
            var a = sim.aircraft[i];
            if (a.phase === 'grounded') {
                updatePlaneVisual(a);
                continue;
            }
            if (a.phase === 'stand') {
                a.waitLeft -= dt;
                a.pos = {lat: a.pos.lat, lng: a.pos.lng, bearing: a.pos.bearing};
                if (a.waitLeft <= 0) beginTaxiOut(a);
            } else {
                var speed = a.phase === 'takeoff' ? taxiMps * 3.5 : taxiMps;
                a.distance += speed * dt;
                a.pos = pointAt(a.points, a.cum, a.distance);
                if (a.distance >= a.cum.total - 0.5) {
                    if (a.phase === 'taxi_in') {
                        a.phase = 'stand';
                        a.waitLeft = Math.max(1, sim.params.dwell);
                        a.standNodeId = sim.graph.stands[a.standId].nodeId;
                    } else if (a.phase === 'taxi_out') {
                        beginTakeoff(a);
                    } else if (a.phase === 'takeoff') {
                        removeAircraft(a);
                        continue;
                    }
                }
            }
            updatePlaneVisual(a);
        }
        sim.rafId = requestAnimationFrame(tick);
    }

    // ---------- публичный интерфейс ----------
    function setStatus(text) {
        if (sim.options.onStatus) sim.options.onStatus(text);
    }

    function setParams(partial) {
        if (partial) {
            for (var k in partial) {
                if (partial.hasOwnProperty(k) && DEFAULTS.hasOwnProperty(k)) {
                    sim.params[k] = partial[k];
                }
            }
        }
        // параметры интерфейса применяем сразу
        if (sim.layoutLayer) {
            if (sim.params.showLayout) {
                if (!sim.map.hasLayer(sim.layoutLayer)) sim.layoutLayer.addTo(sim.map);
            } else if (sim.map.hasLayer(sim.layoutLayer)) {
                sim.map.removeLayer(sim.layoutLayer);
            }
        }
        for (var i = 0; i < sim.aircraft.length; i++) updatePlaneVisual(sim.aircraft[i]);
        while (sim.aircraft.length > sim.params.count) removeAircraft(sim.aircraft[sim.aircraft.length - 1]);
    }

    function getParams() {
        var out = {};
        for (var k in sim.params) if (sim.params.hasOwnProperty(k)) out[k] = sim.params[k];
        return out;
    }

    function setAircraftPool(list) {
        var next = Array.isArray(list) ? list.filter(Boolean) : [];
        // список обновляется каждые 2 секунды вместе с заявками;
        // не сбрасываем очередь подписей, иначе все борта получат одно имя
        if (next.join('|') !== sim.pool.join('|')) sim.poolIndex = 0;
        sim.pool = next;
    }

    function setGrounded(list) {
        var next = {};
        if (Array.isArray(list)) {
            for (var i = 0; i < list.length; i++) if (list[i]) next[list[i]] = true;
        }
        sim.grounded = next;
    }

    function start() {
        if (!sim.ready || sim.running) return;
        sim.running = true;
        sim.lastFrame = 0;
        sim.nextSpawnAt = 0;
        setStatus('Симуляция запущена');
        sim.rafId = requestAnimationFrame(tick);
    }

    function pause() {
        sim.running = false;
        if (sim.rafId) { cancelAnimationFrame(sim.rafId); sim.rafId = null; }
        setStatus('Пауза');
    }

    function reset() {
        pause();
        while (sim.aircraft.length) removeAircraft(sim.aircraft[sim.aircraft.length - 1]);
        sim.spawnIndex = 0;
        sim.poolIndex = 0;
        sim.nextSpawnAt = 0;
        setStatus('Сброшено');
    }

    function init(map, options) {
        sim.map = map;
        sim.options = options || {};
        sim.params = {};
        for (var k in DEFAULTS) if (DEFAULTS.hasOwnProperty(k)) sim.params[k] = DEFAULTS[k];

        var url = sim.options.pathsUrl || '/static/aircraft_paths.geojson';
        setStatus('Загрузка схемы аэродрома…');
        return fetch(url)
            .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
            .then(function (data) {
                sim.graph = buildGraph(data);
                sim.stats.segments = data.features.length;
                sim.stats.nodes = sim.graph.mainNodes.length;

                var runwayEnds = [];
                for (var i = 0; i < sim.graph.runwayFeatures.length; i++) {
                    var rf = sim.graph.runwayFeatures[i];
                    if (!rf.nodeIds || rf.nodeIds.length < 2) continue;
                    var first = sim.graph.nodes[rf.nodeIds[0]];
                    var last = sim.graph.nodes[rf.nodeIds[rf.nodeIds.length - 1]];
                    var firstSnap = nearestMainNode(sim.graph, first.lng, first.lat);
                    var lastSnap = nearestMainNode(sim.graph, last.lng, last.lat);
                    if (firstSnap.id >= 0) {
                        // взлёт продолжается за торец — наружу от ВПП
                        runwayEnds.push({nodeId: firstSnap.id, lat: first.lat, lng: first.lng,
                            ref: rf.ref, direction: bearingDeg(last, first)});
                    }
                    if (lastSnap.id >= 0) {
                        runwayEnds.push({nodeId: lastSnap.id, lat: last.lat, lng: last.lng,
                            ref: rf.ref, direction: bearingDeg(first, last)});
                    }
                }
                sim.graph.runwayEnds = runwayEnds;

                // стоянки: виртуальный узел + ребро к ближайшей точке сети
                var stands = sim.options.stands || {};
                sim.graph.stands = {};
                sim.graph.standKeys = [];
                Object.keys(stands).forEach(function (ref) {
                    var p = stands[ref];
                    var snap = nearestMainNode(sim.graph, p.lng, p.lat);
                    if (snap.id < 0) return;
                    var nodeId = sim.graph.nodes.length;
                    sim.graph.nodes.push({lat: p.lat, lng: p.lng, edges: [], main: true});
                    var w = Math.max(5, snap.distance);
                    sim.graph.nodes[nodeId].edges.push({to: snap.id, w: w});
                    sim.graph.nodes[snap.id].edges.push({to: nodeId, w: w});
                    sim.graph.stands[ref] = {nodeId: nodeId, lat: p.lat, lng: p.lng, link: snap.distance};
                    sim.graph.standKeys.push(ref);
                });
                sim.stats.stands = sim.graph.standKeys.length;

                sim.layoutLayer = addLayout(map, data);
                sim.standLayer = addStandMarkers(map, stands, sim.options.onStandClick);
                if (sim.graph.bounds) {
                    try {
                        map.fitBounds(sim.graph.bounds, {padding: [24, 24]});
                        // на демо-экране слишком мелкий масштаб: подписи слипаются
                        if (map.getZoom() < 13) {
                            map.setView(map.getCenter(), 13, {animate: false});
                        }
                    } catch (e) {}
                }
                sim.ready = true;
                setStatus('Схема готова: ' + sim.stats.segments + ' сегментов, ' +
                    sim.stats.nodes + ' узлов, ' + sim.stats.stands + ' стоянок');
                return sim.stats;
            })
            .catch(function (err) {
                setStatus('Схема не загружена: ' + err.message);
                throw err;
            });
    }

    window.AirportSim = {
        init: init,
        start: start,
        pause: pause,
        reset: reset,
        setParams: setParams,
        getParams: getParams,
        setAircraftPool: setAircraftPool,
        setGrounded: setGrounded,
        isReady: function () { return sim.ready; },
        isRunning: function () { return sim.running; },
        stats: function () { return {ready: sim.ready, running: sim.running, aircraft: sim.aircraft.length,
            nodes: sim.stats.nodes, stands: sim.stats.stands, segments: sim.stats.segments}; },
        _sim: sim
    };
})();
