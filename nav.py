"""Map model: 2m height grid, line of sight, walkability, and incremental 4m flow field.

Built at startup from v2/world.json and v2/terrain.i16 (protocol.md section 11).
Standard library only.
"""
import array
import heapq
import json
import math
import os
import time

RES = 2.0          # fine grid (LOS, local steering), meters per cell
CRES = 4.0         # coarse grid (flow fields), meters per cell
CLEAR = 9.0        # mech height: overhangs with this much clearance can be walked under
INF = 1e18


class Map:
    def __init__(self, map_dir):
        t0 = time.perf_counter()
        with open(os.path.join(map_dir, "v2", "world.json")) as f:
            w = json.load(f)
        ter = w["terrain"]
        self.tminx, self.tminy = ter["minX"], ter["minY"]
        self.tnx, self.tny = ter["nx"], ter["ny"]
        tstep = ter["step"]
        raw = array.array("h")
        with open(os.path.join(map_dir, "v2", ter.get("file", "terrain.i16")), "rb") as f:
            raw.frombytes(f.read())
        if raw.itemsize == 2 and array.array("h", [1]).tobytes() != b"\x01\x00":
            raw.byteswap()
        scale = ter.get("scale", 0.01)
        self.terr = raw
        self.tscale = scale
        self.tstep = tstep
        b = w["bounds"]
        self.bminx, self.bmaxx, self.bminy, self.bmaxy = b["minX"], b["maxX"], b["minY"], b["maxY"]

        self.minx, self.miny = self.tminx, self.tminy
        nx = int((self.tnx - 1) * tstep / RES)
        ny = int((self.tny - 1) * tstep / RES)
        self.nx, self.ny = nx, ny
        n = nx * ny
        ground = [0.0] * n
        tnx = self.tnx
        for j in range(ny):
            yy = (RES * j + RES / 2) / tstep
            j0 = int(yy)
            fy = yy - j0
            base0 = j0 * tnx
            base1 = base0 + tnx
            row = j * nx
            for i in range(nx):
                xx = (RES * i + RES / 2) / tstep
                i0 = int(xx)
                fx = xx - i0
                a = raw[base0 + i0]
                bb = raw[base0 + i0 + 1]
                c = raw[base1 + i0]
                d = raw[base1 + i0 + 1]
                ground[row + i] = ((a * (1 - fx) + bb * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy) * scale
        self.ground = ground
        top = [-INF] * n
        over = {}
        minx, miny = self.minx, self.miny
        for p in w["prisms"]:
            pb, pt = p["base"], p["top"]
            edges = []
            for ring in [p["ring"]] + list(p.get("holes") or []):
                m = len(ring) // 2
                for k in range(m):
                    x0, y0 = ring[2 * k], ring[2 * k + 1]
                    x1, y1 = ring[(2 * k + 2) % (2 * m)], ring[(2 * k + 3) % (2 * m)]
                    if y0 != y1:
                        edges.append((x0, y0, x1, y1))
            if not edges:
                continue
            ys = [e[1] for e in edges] + [e[3] for e in edges]
            j_lo = max(0, int((min(ys) - miny) / RES) - 1)
            j_hi = min(ny - 1, int((max(ys) - miny) / RES) + 1)
            for j in range(j_lo, j_hi + 1):
                y = miny + RES * j + RES / 2
                xs = []
                for (x0, y0, x1, y1) in edges:
                    if (y0 <= y < y1) or (y1 <= y < y0):
                        xs.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
                if len(xs) < 2:
                    continue
                xs.sort()
                row = j * nx
                for k in range(0, len(xs) - 1, 2):
                    i0 = max(0, int(math.ceil((xs[k] - minx - RES / 2) / RES)))
                    i1 = min(nx - 1, int(math.floor((xs[k + 1] - minx - RES / 2) / RES)))
                    for i in range(i0, i1 + 1):
                        c = row + i
                        g = ground[c]
                        if pt <= g + 0.05:
                            continue
                        if pb - g >= CLEAR:
                            over.setdefault(c, []).append((pb, pt))
                        elif pt > top[c]:
                            top[c] = pt
        self.over = over
        surf = [0.0] * n
        for c in range(n):
            t = top[c]
            g = ground[c]
            surf[c] = t if t > g else g
        self.surf = surf
        self.top = top
        wall = bytearray(n)
        for j in range(ny):
            row = j * nx
            for i in range(nx):
                c = row + i
                s = surf[c]
                if i + 1 < nx and abs(surf[c + 1] - s) > 1.3:
                    wall[c] = 1
                    wall[c + 1] = 1
                if j + 1 < ny and abs(surf[c + nx] - s) > 1.3:
                    wall[c] = 1
                    wall[c + nx] = 1
        self.wall = wall
        self._build_coarse()
        self.build_s = time.perf_counter() - t0

    def cell(self, x, y):
        i = int((x - self.minx) / RES)
        j = int((y - self.miny) / RES)
        if i < 0:
            i = 0
        elif i >= self.nx:
            i = self.nx - 1
        if j < 0:
            j = 0
        elif j >= self.ny:
            j = self.ny - 1
        return j * self.nx + i

    def inside(self, x, y, margin=3.0):
        return self.bminx + margin < x < self.bmaxx - margin and self.bminy + margin < y < self.bmaxy - margin

    def surf_at(self, x, y):
        return self.surf[self.cell(x, y)]

    def ground_at(self, x, y):
        return self.ground[self.cell(x, y)]

    def los(self, ax, ay, az, bx, by, bz, step=1.5):
        """True if the straight segment a->b clears buildings and terrain (trees ignored)."""
        dx, dy, dz = bx - ax, by - ay, bz - az
        d = math.sqrt(dx * dx + dy * dy)
        nsteps = int(d / step) + 1
        if nsteps < 2:
            return True
        minx, miny, nx, ny = self.minx, self.miny, self.nx, self.ny
        surf, over = self.surf, self.over
        inv = 1.0 / nsteps
        for k in range(1, nsteps):
            t = k * inv
            x = ax + dx * t
            y = ay + dy * t
            z = az + dz * t
            i = int((x - minx) / RES)
            j = int((y - miny) / RES)
            if i < 0 or j < 0 or i >= nx or j >= ny:
                return False
            c = j * nx + i
            if z < surf[c]:
                return False
            if over:
                ov = over.get(c)
                if ov:
                    for (b0, t0) in ov:
                        if b0 < z < t0:
                            return False
        return True

    def _build_coarse(self):
        f = int(CRES / RES)
        cnx, cny = self.nx // f, self.ny // f
        self.cnx, self.cny = cnx, cny
        n = cnx * cny
        csurf = [0.0] * n
        cmix = bytearray(n)
        nx = self.nx
        surf = self.surf
        for J in range(cny):
            for I in range(cnx):
                vals = []
                for dj in range(f):
                    row = (J * f + dj) * nx + I * f
                    for di in range(f):
                        vals.append(surf[row + di])
                lo = min(vals)
                hi = max(vals)
                csurf[J * cnx + I] = lo
                if hi - lo > 1.3:
                    cmix[J * cnx + I] = 1
        self.csurf = csurf
        self.cmix = cmix
        nbrs = [None] * n
        for J in range(cny):
            for I in range(cnx):
                c = J * cnx + I
                s = csurf[c]
                lst = []
                for dI, dJ, w in ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                                  (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414)):
                    I2, J2 = I + dI, J + dJ
                    if 0 <= I2 < cnx and 0 <= J2 < cny:
                        c2 = J2 * cnx + I2
                        if csurf[c2] - s <= 1.6:
                            cost = w * CRES * (2.5 if cmix[c2] else 1.0)
                            lst.append((c2, cost))
                nbrs[c] = lst
        self.cnbrs = nbrs

    def ccell(self, x, y):
        I = int((x - self.minx) / CRES)
        J = int((y - self.miny) / CRES)
        I = 0 if I < 0 else (self.cnx - 1 if I >= self.cnx else I)
        J = 0 if J < 0 else (self.cny - 1 if J >= self.cny else J)
        return J * self.cnx + I

    def ccenter(self, c):
        J, I = divmod(c, self.cnx)
        return self.minx + CRES * I + CRES / 2, self.miny + CRES * J + CRES / 2


class FlowField:
    """Distance-to-goal field on the coarse grid, computed a slice at a time (reverse Dijkstra)."""

    def __init__(self, m, goal_cells, key=None):
        self.m = m
        self.key = key
        n = m.cnx * m.cny
        self.dist = [INF] * n
        self.heap = []
        for c in goal_cells:
            self.dist[c] = 0.0
            self.heap.append((0.0, c))
        heapq.heapify(self.heap)
        self.done = not self.heap

    def step(self, budget_s):
        if self.done:
            return True
        t_end = time.perf_counter() + budget_s
        heap, dist, nbrs = self.heap, self.dist, self.m.cnbrs
        pop, push = heapq.heappop, heapq.heappush
        k = 0
        while heap:
            d, c = pop(heap)
            if d > dist[c]:
                continue
            for c2, w in nbrs[c]:
                nd = d + w
                if nd < dist[c2]:
                    dist[c2] = nd
                    push(heap, (nd, c2))
            k += 1
            if (k & 255) == 0 and time.perf_counter() > t_end:
                return False
        self.done = True
        return True

    def direction(self, x, y):
        m = self.m
        c = m.ccell(x, y)
        d0 = self.dist[c]
        cnx = m.cnx
        J, I = divmod(c, cnx)
        best = None
        bestd = d0
        for R in (1, 2):
            for dJ in range(-R, R + 1):
                for dI in range(-R, R + 1):
                    if max(abs(dI), abs(dJ)) != R:
                        continue
                    I2, J2 = I + dI, J + dJ
                    if 0 <= I2 < cnx and 0 <= J2 < m.cny:
                        c2 = J2 * cnx + I2
                        dd = self.dist[c2]
                        if dd < INF and dd < bestd - 0.01 * R:
                            bestd = dd
                            best = c2
            if best is not None and R == 1:
                break
        if best is None:
            return None
        bx, by = m.ccenter(best)
        dx, dy = bx - x, by - y
        L = math.hypot(dx, dy)
        if L < 1e-6:
            return None
        return dx / L, dy / L
