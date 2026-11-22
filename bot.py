"""Mech Royale bot — heuristic behavior AI for the mechfight/1 battle royale.

Design goals (in priority order):
  1. Never crash, never emit non-protocol output, always answer well under 50 ms.
  2. Never die for free: stay inside the (next) zone circle with building-aware motion.
  3. Win favorable fights with lead-aimed gun fire; face threats to deny rear armor.
  4. Snowball: pick up cubes, laser loot, capture supply drops, pick smart upgrades.
  5. Retreat to regen when hurt; dodge mortars / charging lasers.

Everything is pure stdlib Python so the Docker image needs no packages.
"""
from __future__ import annotations

import math
import os
import json
import array
import random
from typing import Any, Dict, List, Optional, Tuple

import mechfight as mf

TAU = math.tau
INF = float("inf")


def wrap(a: float) -> float:
    """Wrap angle to [-pi, pi]."""
    return (a + math.pi) % TAU - math.pi


def clampf(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def point_in_ring(x: float, y: float, ring: List[float]) -> bool:
    """Ray-cast point-in-polygon for a flat [x0,y0,x1,y1,...] ring."""
    n = len(ring)
    if n < 6:
        return False
    inside = False
    j = n - 2
    for i in range(0, n, 2):
        xi, yi = ring[i], ring[i + 1]
        xj, yj = ring[j], ring[j + 1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


class Prism:
    __slots__ = ("base", "top", "ring", "holes", "minx", "miny", "maxx", "maxy")

    def __init__(self, base: float, top: float, ring: List[float], holes: List[List[float]]):
        self.base = base
        self.top = top
        self.ring = ring
        self.holes = holes
        xs = ring[0::2]
        ys = ring[1::2]
        self.minx, self.maxx = min(xs), max(xs)
        self.miny, self.maxy = min(ys), max(ys)

    def contains(self, x: float, y: float, z: float) -> bool:
        if not (self.base <= z <= self.top):
            return False
        if not (self.minx <= x <= self.maxx and self.miny <= y <= self.maxy):
            return False
        if not point_in_ring(x, y, self.ring):
            return False
        for h in self.holes:
            if point_in_ring(x, y, h):
                return False
        return True


class WorldModel:
    """Parses the static map once. Solid prisms (buildings/stadium) occlude shots.

    Bridges have a base above the ground (walk under), so we treat only their solid
    slab as an occluder between base and top. Trees never stop shots, so we ignore
    them for line-of-sight of fire (they only fade vision, which the server handles)."""

    def __init__(self, hello: mf.Hello):
        self.prisms: List[Prism] = []
        self.ter: Optional[array.array] = None
        self.nx = self.ny = 0
        self.minX = self.minY = 0.0
        self.step = 1.0
        try:
            self._load(hello)
        except Exception as e:  # noqa: BLE001
            mf.log(f"WorldModel load failed (continuing blind): {e}")

    def _load(self, hello: mf.Hello) -> None:
        d = hello.get("map", {}).get("dir") or "/arena"
        wpath = os.path.join(d, "v2", "world.json")
        with open(wpath, "r") as f:
            w = json.load(f)
        for p in w.get("prisms", []):
            kind = p.get("kind")
            if kind not in ("building", "stadium", "bridge"):
                continue
            ring = p.get("ring") or []
            if len(ring) < 6:
                continue
            self.prisms.append(Prism(float(p.get("base", -1e3)), float(p.get("top", 1e3)),
                                     ring, p.get("holes") or []))
        t = w.get("terrain") or {}
        self.nx = int(t.get("nx", 0))
        self.ny = int(t.get("ny", 0))
        self.minX = float(t.get("minX", 0.0))
        self.minY = float(t.get("minY", 0.0))
        self.step = float(t.get("step", 1.0)) or 1.0
        tpath = os.path.join(d, "v2", "terrain.i16")
        if self.nx and self.ny and os.path.exists(tpath):
            with open(tpath, "rb") as f:
                raw = f.read()
            a = array.array("h")
            a.frombytes(raw[: self.nx * self.ny * 2])
            if "little" != os.sys.byteorder:  # i16 is little-endian on disk
                a.byteswap()
            self.ter = a
        mf.log(f"WorldModel: {len(self.prisms)} prisms, terrain={'yes' if self.ter else 'no'}")

    def ground_z(self, x: float, y: float) -> float:
        if not self.ter:
            return 0.0
        fi = (x - self.minX) / self.step
        fj = (y - self.minY) / self.step
        i0 = int(math.floor(fi))
        j0 = int(math.floor(fj))
        if i0 < 0 or j0 < 0 or i0 >= self.nx - 1 or j0 >= self.ny - 1:
            return 0.0
        tx = fi - i0
        ty = fj - j0
        g = self.ter
        nx = self.nx
        h00 = g[j0 * nx + i0]
        h10 = g[j0 * nx + i0 + 1]
        h01 = g[(j0 + 1) * nx + i0]
        h11 = g[(j0 + 1) * nx + i0 + 1]
        top = h00 + (h10 - h00) * tx
        bot = h01 + (h11 - h01) * tx
        return (top + (bot - top) * ty) / 100.0

    def seg_blocked(self, a: Tuple[float, float, float], b: Tuple[float, float, float]) -> bool:
        """True if a building/stadium/bridge solid blocks the segment a->b."""
        if not self.prisms:
            return False
        ax, ay, az = a
        bx, by, bz = b
        sminx, smaxx = (ax, bx) if ax < bx else (bx, ax)
        sminy, smaxy = (ay, by) if ay < by else (by, ay)
        cand = [p for p in self.prisms
                if not (p.maxx < sminx or p.minx > smaxx or p.maxy < sminy or p.miny > smaxy)]
        if not cand:
            return False
        dist = math.hypot(bx - ax, by - ay)
        n = int(dist / 3.0) + 2
        for k in range(1, n):
            t = k / n
            x = ax + (bx - ax) * t
            y = ay + (by - ay) * t
            z = az + (bz - az) * t
            for p in cand:
                if p.contains(x, y, z):
                    return True
        return False


class Bot:
    name = "lockin"

    def __init__(self, hello: mf.Hello):
        self.world = WorldModel(hello)
        cfg = hello.get("config", {}) or {}
        self.cfg = cfg
        wp = (cfg.get("weapons", {}) or {})
        self.gun_speed = float((wp.get("gun", {}) or {}).get("speed", 252.0))
        self.gun_range = float((wp.get("gun", {}) or {}).get("range", 230.0))
        self.gun_spread = math.radians(float((wp.get("gun", {}) or {}).get("spread_deg", 0.6)))
        mortar = wp.get("mortar", {}) or {}
        self.mortar_gravity = float(mortar.get("gravity", 22.0))
        self.mortar_speed = float(mortar.get("speed", 72.0))
        self.laser_charge_full = float((wp.get("laser", {}) or {}).get("charge", 0.75))
        self.zone_dps_cfg = cfg.get("zone", {})
        self.rng = random.Random(int(hello.get("seed", 0)))
        # state
        self.prev_pos: Optional[List[float]] = None
        self.stuck_ticks = 0
        self.last_threat_yaw: Optional[float] = None
        self.strafe_sign = 1
        self.last_strafe_flip = 0.0
        self.aim_cmd = [0.0, 0.0]

    # ---- helpers -------------------------------------------------------
    def _los(self, me_pos, muzzle_h, tpos, tz_off) -> bool:
        a = (me_pos[0], me_pos[1], me_pos[2] + muzzle_h)
        b = (tpos[0], tpos[1], tpos[2] + tz_off)
        return not self.world.seg_blocked(a, b)

    def _lead_aim(self, me_pos, muzzle_h, tpos, tvel, proj_speed, aim_z_off):
        """Iterative projectile-lead solution. Returns [yaw, pitch]."""
        px, py, pz = tpos[0], tpos[1], tpos[2] + aim_z_off
        mx, my, mz = me_pos[0], me_pos[1], me_pos[2] + muzzle_h
        vx, vy = (tvel[0] if tvel else 0.0), (tvel[1] if tvel else 0.0)
        t = 0.0
        for _ in range(3):
            fx = px + vx * t
            fy = py + vy * t
            d = math.hypot(fx - mx, fy - my)
            t = d / max(proj_speed, 1.0)
        fx = px + vx * t
        fy = py + vy * t
        dx = fx - mx
        dy = fy - my
        dz = pz - mz  # gun has no drop; aim straight at target height
        yaw = math.atan2(dy, dx)
        horiz = math.hypot(dx, dy)
        pitch = math.atan2(dz, max(horiz, 1e-3))
        return [yaw, pitch]

    def _zone_safe_point(self, obs, pos):
        """Return (need_move, point) to stay inside the circle we must respect."""
        z = obs.get("zone") or {}
        t = obs.get("t", 0.0)
        cur_c = z.get("center", [0.0, 0.0])
        cur_r = float(z.get("radius", 1e4))
        nxt_c = z.get("next_center", cur_c)
        nxt_r = float(z.get("next_radius", cur_r))
        shrink_start = float(z.get("shrink_start", t + 1e4))
        # If shrink is near or underway, commit to the next circle; else current.
        if t >= shrink_start - 12.0:
            cx, cy, r = nxt_c[0], nxt_c[1], nxt_r
        else:
            cx, cy, r = cur_c[0], cur_c[1], cur_r
        margin = max(10.0, r * 0.12)
        d = math.hypot(pos[0] - cx, pos[1] - cy)
        safe_r = max(5.0, r - margin)
        if d <= safe_r:
            return (False, [cx, cy])
        # aim to a point safe_r*0.7 in from the edge along bearing to center
        ang = math.atan2(cy - pos[1], cx - pos[0])
        tx = cx - math.cos(ang) * (safe_r * 0.6)
        ty = cy - math.sin(ang) * (safe_r * 0.6)
        return (True, [tx, ty])

    @staticmethod
    def _dist2(a, b) -> float:
        dx = a[0] - b[0]
        dy = a[1] - b[1]
        return dx * dx + dy * dy

    def _pick_upgrade(self, offer) -> int:
        choices = offer.get("choices", []) or []
        if not choices:
            return 0
        # priority by keyword in id/name/desc
        order = ["armor", "reactive", "hp", "health", "regen",
                 "gun", "rotary", "rate", "damage", "dmg",
                 "sensor", "vision", "speed", "boost", "energy"]
        best_i, best_rank = 0, 10 ** 9
        for i, c in enumerate(choices):
            text = (str(c.get("id", "")) + " " + str(c.get("name", "")) + " " + str(c.get("desc", ""))).lower()
            rank = 10 ** 9
            for r, kw in enumerate(order):
                if kw in text:
                    rank = r
                    break
            if rank < best_rank:
                best_rank, best_i = rank, i
        return best_i

    # ---- main ----------------------------------------------------------
    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        pos = me["pos"]
        vel = me.get("vel", [0, 0, 0])
        hp = float(me.get("hp", 1))
        max_hp = float(me.get("max_hp", 1)) or 1.0
        hp_frac = hp / max_hp
        energy = float(me.get("energy", 0))
        stats = me.get("stats", {}) or {}
        muzzle_h = float(stats.get("muzzle_height", 7.0))
        weapons = me.get("weapons", []) or []
        t_now = obs.get("t", 0.0)
        tick = obs["tick"]

        act: mf.Action = {"tick": tick}

        # Always resolve upgrade offers (deliberately).
        if obs.get("offer"):
            act["pick"] = self._pick_upgrade(obs["offer"])

        # --- gather actors ---
        enemies = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]
        npcs = [m for m in obs.get("mechs", []) if m.get("kind") == "npc"]

        # --- zone safety ---
        need_zone, zpoint = self._zone_safe_point(obs, pos)
        in_zone = me.get("in_zone", True)

        # --- threats ---
        # nearest enemy by distance
        enemies.sort(key=lambda m: self._dist2(m["pos"], pos))
        nearest = enemies[0] if enemies else None
        nearest_d = math.sqrt(self._dist2(nearest["pos"], pos)) if nearest else INF

        # incoming mortar shells heading roughly at us
        incoming_mortar = None
        for p in obs.get("projectiles", []):
            if p.get("kind") == "mortar":
                dx = pos[0] - p["pos"][0]
                dy = pos[1] - p["pos"][1]
                pv = p.get("vel", [0, 0, 0])
                if dx * pv[0] + dy * pv[1] > 0 and (dx * dx + dy * dy) < 60 * 60:
                    incoming_mortar = p
                    break
        # charging enemy laser pointed near us
        laser_threat = None
        for m in enemies:
            if m.get("charging"):
                mp = m["pos"]
                to_me = math.atan2(pos[1] - mp[1], pos[0] - mp[0])
                aim = m.get("aim", [0, 0])
                if abs(wrap(aim[0] - to_me)) < 0.25:
                    laser_threat = m
                    break

        # track threat bearing from damage events
        for ev in obs.get("events", []):
            if ev.get("type") == "damaged" and ev.get("bearing") is not None:
                self.last_threat_yaw = ev["bearing"]

        # --- choose primary combat target ---
        target = self._choose_target(me, pos, enemies, npcs, hp_frac)

        # default aim: toward target, else toward threat, else travel dir
        desired_aim = list(me.get("aim", [0.0, 0.0]))
        fire = [False, False]
        move = [0.0, 0.0]
        boost = False
        jump = False

        # ========== MODE SELECTION (priority) ==========
        mode = "reposition"

        # 1) hard zone emergency: far outside -> rush in
        if need_zone and not in_zone:
            mode = "zone"
        # 2) dodge imminent mortar / laser
        elif incoming_mortar is not None or laser_threat is not None:
            mode = "dodge"
        # 3) retreat & heal when low and pressured
        elif hp_frac < 0.33 and nearest is not None and nearest_d < 130:
            mode = "retreat"
        # 4) engage a target
        elif target is not None:
            mode = "engage"
        # 5) farm / loot when calm
        else:
            mode = "farm"

        # ========== MODE EXECUTION ==========
        if mode == "zone":
            move = self._dir_to(pos, zpoint)
            boost = energy > 20
            if target:
                desired_aim = self._aim_at(me, muzzle_h, target)
            elif self.last_threat_yaw is not None:
                desired_aim = [self.last_threat_yaw, 0.0]

        elif mode == "dodge":
            # strafe perpendicular to the incoming threat, break toward safety
            if incoming_mortar is not None:
                src = incoming_mortar["pos"]
            else:
                src = laser_threat["pos"]
            away = math.atan2(pos[1] - src[1], pos[0] - src[0])
            perp = away + math.pi / 2 * self._strafe(t_now)
            move = [math.cos(perp), math.sin(perp)]
            # bias toward zone center so we don't strafe out
            if need_zone:
                zc = self._dir_to(pos, zpoint)
                move = [move[0] + zc[0], move[1] + zc[1]]
            boost = energy > 25
            if target:
                desired_aim = self._aim_at(me, muzzle_h, target)
                fire = self._maybe_fire(me, muzzle_h, target, weapons, desired_aim)

        elif mode == "retreat":
            # move away from nearest threat, toward zone center, break LoS
            thy = self.last_threat_yaw
            if nearest is not None:
                thy = math.atan2(nearest["pos"][1] - pos[1], nearest["pos"][0] - pos[0])
            if thy is None:
                thy = 0.0
            away = thy + math.pi
            dirx, diry = math.cos(away), math.sin(away)
            if need_zone:
                zc = self._dir_to(pos, zpoint)
                dirx += zc[0] * 1.5
                diry += zc[1] * 1.5
            move = [dirx, diry]
            boost = energy > 20
            # keep facing the threat to protect rear; fire if a clean shot exists
            if nearest is not None:
                desired_aim = self._aim_at(me, muzzle_h, nearest)
                if nearest_d < 90:
                    fire = self._maybe_fire(me, muzzle_h, nearest, weapons, desired_aim)

        elif mode == "engage":
            desired_aim = self._aim_at(me, muzzle_h, target)
            fire = self._maybe_fire(me, muzzle_h, target, weapons, desired_aim)
            move, boost = self._kite(me, pos, energy, target, need_zone, zpoint, t_now)

        else:  # farm / reposition
            goal = self._farm_goal(me, pos, obs, npcs, need_zone, zpoint)
            move = self._dir_to(pos, goal)
            boost = (not in_zone) and energy > 25
            # take nearby loot / cubes
            self._maybe_take(act, me, pos, obs, weapons)
            # scan: aim toward travel to widen vision, or toward last threat
            if self.last_threat_yaw is not None and enemies:
                desired_aim = [self.last_threat_yaw, 0.0]
            else:
                desired_aim = [math.atan2(move[1], move[0]) if (move[0] or move[1]) else desired_aim[0], 0.0]

        # take loot opportunistically in combat-free movement too
        if mode in ("engage", "retreat", "zone"):
            self._maybe_take(act, me, pos, obs, weapons)

        # reload when safe and low on ammo
        w0 = weapons[0] if weapons else None
        if w0 and w0.get("kind") == "gun":
            ammo = w0.get("ammo", 99)
            if ammo is not None and ammo <= 2 and (target is None or nearest_d > 120):
                act["reload"] = True

        # --- stuck detection: jump/jet to unstick ---
        jump, boost = self._unstick(pos, move, boost, jump, energy)

        # normalize move
        ml = math.hypot(move[0], move[1])
        if ml > 1e-6:
            move = [move[0] / ml, move[1] / ml]
        else:
            move = [0.0, 0.0]

        # clamp pitch
        desired_aim[1] = clampf(desired_aim[1], -1.4, 1.4)
        self.aim_cmd = desired_aim

        act.update(move=move, aim=desired_aim, boost=bool(boost), jump=bool(jump), fire=fire)

        # debug
        try:
            mf.draw.label(f"{mode} hp{hp:.0f} e{energy:.0f} a{obs.get('alive','?')}")
            if target:
                tp = target["pos"]
                mf.draw.line([pos[0], pos[1], pos[2] + muzzle_h], [tp[0], tp[1], tp[2] + 4.0],
                             "#ff5040" if any(fire) else "#ffd060")
        except Exception:  # noqa: BLE001
            pass

        self.prev_pos = list(pos)
        return act

    # ---- sub-behaviors -------------------------------------------------
    def _choose_target(self, me, pos, enemies, npcs, hp_frac):
        """Pick a target worth shooting: visible, in range, favorable."""
        muzzle_h = float((me.get("stats", {}) or {}).get("muzzle_height", 7.0))
        best = None
        best_score = -INF
        for m in enemies:
            d = math.sqrt(self._dist2(m["pos"], pos))
            if d > self.gun_range + 10:
                continue
            if not self._los(pos, muzzle_h, m["pos"], 4.0):
                continue
            mhp = m.get("hp", 1400)
            # prefer low hp, close targets; penalize distance
            score = 1200.0 - mhp - d * 2.0
            if score > best_score:
                best_score, best = score, m
        # consider NPCs for cube farming only when healthy and nothing better close
        if best is None and hp_frac > 0.55:
            for m in npcs:
                d = math.sqrt(self._dist2(m["pos"], pos))
                if d < 70 and self._los(pos, muzzle_h, m["pos"], 1.5):
                    if best is None or d < math.sqrt(self._dist2(best["pos"], pos)):
                        best = m
        return best

    def _aim_at(self, me, muzzle_h, target):
        tvel = target.get("vel", [0, 0, 0])
        return self._lead_aim(me["pos"], muzzle_h, target["pos"], tvel, self.gun_speed, 4.0)

    def _maybe_fire(self, me, muzzle_h, target, weapons, desired_aim):
        fire = [False, False]
        if target is None:
            return fire
        d = math.sqrt(self._dist2(target["pos"], me["pos"]))
        cur = me.get("aim", desired_aim)
        yaw_err = abs(wrap(desired_aim[0] - cur[0]))
        pitch_err = abs(desired_aim[1] - cur[1])
        if not self._los(me["pos"], muzzle_h, target["pos"], 4.0):
            return fire
        # gun (slot 0)
        w0 = weapons[0] if len(weapons) > 0 else None
        if w0 and w0.get("kind") == "gun":
            ammo = w0.get("ammo", 1)
            cd = w0.get("cooldown", 0)
            tol = max(self.gun_spread * 3.0, 0.045) + min(0.06, d * 0.0005)
            if (ammo is None or ammo > 0) and yaw_err < tol and pitch_err < 0.12 and d <= self.gun_range:
                fire[0] = True
        # slot 1 weapon
        w1 = weapons[1] if len(weapons) > 1 else None
        if w1:
            k = w1.get("kind")
            cd = w1.get("cooldown", 0)
            if k == "lightning":
                if d <= 44 and yaw_err < 0.26 and (cd is None or cd <= 0):
                    fire[1] = True
            elif k == "laser":
                # charge while roughly aimed; the server fires on release / full hold
                charge = w1.get("charge", 0.0) or 0.0
                if d <= 270 and yaw_err < 0.1 and pitch_err < 0.1:
                    # hold to charge until near full, then release (fire False) to shoot
                    fire[1] = charge < self.laser_charge_full * 0.92
                else:
                    fire[1] = False
            elif k == "mortar":
                # only lob when target is far enough that our own splash won't hit
                if 25 < d <= 200 and yaw_err < 0.12 and (cd is None or cd <= 0):
                    fire[1] = True
        return fire

    def _kite(self, me, pos, energy, target, need_zone, zpoint, t_now):
        tp = target["pos"]
        d = math.sqrt(self._dist2(tp, pos))
        to_t = math.atan2(tp[1] - pos[1], tp[0] - pos[0])
        pref = 70.0
        if d < pref - 12:
            base = to_t + math.pi  # back off
        elif d > pref + 25:
            base = to_t  # close in
        else:
            base = to_t + math.pi / 2 * self._strafe(t_now)  # orbit
        dirx, diry = math.cos(base), math.sin(base)
        if need_zone:
            zc = self._dir_to(pos, zpoint)
            dirx += zc[0]
            diry += zc[1]
        boost = energy > 35 and (d < pref - 12 or d > pref + 40)
        return [dirx, diry], boost

    def _strafe(self, t_now):
        if t_now - self.last_strafe_flip > 1.3:
            self.last_strafe_flip = t_now
            if self.rng.random() < 0.5:
                self.strafe_sign *= -1
        return self.strafe_sign

    def _farm_goal(self, me, pos, obs, npcs, need_zone, zpoint):
        """Where to move when no fight: supply drop > cube > crate > laser pickup > zone center."""
        if need_zone:
            return zpoint
        best = None
        best_d = INF
        # supply drops that are capturable/landing and inside safety
        for dpt in obs.get("drops", []):
            if dpt.get("holder") not in (None, me.get("id")):
                continue
            lands = dpt.get("lands_in", 0)
            if lands is not None and lands > 20:
                continue
            d = math.sqrt(self._dist2(dpt["pos"], pos))
            if d < 160 and d < best_d:
                best_d, best = d, dpt["pos"]
        if best is not None:
            return [best[0], best[1]]
        # cubes
        for c in obs.get("cubes", []):
            d = math.sqrt(self._dist2(c["pos"], pos))
            if d < best_d and d < 90:
                best_d, best = d, c["pos"]
        # nearby laser pickup if slot1 empty
        w = me.get("weapons", []) or []
        slot1_empty = len(w) < 2 or w[1] is None
        if slot1_empty:
            for p in obs.get("pickups", []):
                if p.get("kind") in ("laser", "lightning", "mortar"):
                    d = math.sqrt(self._dist2(p["pos"], pos))
                    if d < best_d and d < 90:
                        best_d, best = d, p["pos"]
        # crates
        for c in obs.get("crates", []):
            d = math.sqrt(self._dist2(c["pos"], pos))
            if d < best_d and d < 70:
                best_d, best = d, c["pos"]
        if best is not None:
            return [best[0], best[1]]
        # drift toward center of safe circle
        return zpoint

    def _maybe_take(self, act, me, pos, obs, weapons):
        """Take a weapon pickup if we're on top of it."""
        reach = float((self.cfg.get("loot", {}) or {}).get("pickup_radius", 4.0))
        w = weapons
        slot1_empty = len(w) < 2 or w[1] is None
        pref = {"laser": 3, "lightning": 2, "mortar": 1}
        best = None
        best_rank = -1
        for p in obs.get("pickups", []):
            if math.sqrt(self._dist2(p["pos"], pos)) > reach:
                continue
            k = p.get("kind")
            if k == "gun":
                continue
            r = pref.get(k, 0)
            if r > best_rank:
                best_rank, best = r, p
        if best is None:
            return
        if slot1_empty:
            act["take"] = best["id"]
        # else: keep what we have (avoid risky mid-fight swaps for v1)

    def _dir_to(self, pos, goal):
        dx = goal[0] - pos[0]
        dy = goal[1] - pos[1]
        n = math.hypot(dx, dy)
        if n < 1e-6:
            return [0.0, 0.0]
        return [dx / n, dy / n]

    def _unstick(self, pos, move, boost, jump, energy):
        """If we intend to move but aren't, jump/jet to clear a ledge."""
        if self.prev_pos is not None and (move[0] or move[1]):
            moved = math.hypot(pos[0] - self.prev_pos[0], pos[1] - self.prev_pos[1])
            if moved < 0.15:
                self.stuck_ticks += 1
            else:
                self.stuck_ticks = max(0, self.stuck_ticks - 2)
        if self.stuck_ticks >= 4:
            jump = True
            if energy > 15:
                boost = True  # jet upward to clear
            if self.stuck_ticks > 14:
                self.stuck_ticks = 0
        return jump, boost


mf.run(Bot)
