"""Mech Royale bot — map-aware survival, cover/peek micro, dual-aim laser+gun combat, flow-field navigation.

Speaks mechfight/1 through the kit wrapper (mechfight.py). Pure Python standard library.
"""
import math
import os
import random
import sys
import time

import mechfight as mf

try:
    import nav
except Exception:  # pragma: no cover
    nav = None

TAU = 2 * math.pi
BUDGET_S = 0.008
RETREAT_HP = 0.40
RECOVER_HP = 0.85

UPGRADE_PRIORITY = [
    "armor", "plating", "nanites", "laser_charge", "laser_damage", "gun_rate", "gun_damage",
    "gun_velocity", "servos", "gyros", "lightning_efficiency", "lightning_range", "gun_mag",
    "capacitor", "jets", "reactor", "sensors", "laser_pierce", "lightning_chain",
    "mortar_reload", "mortar_velocity", "mortar_splash",
]
WEAPON_VALUE = {"laser": 3, "lightning": 2, "mortar": 0, "gun": 0}


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Bot:
    name = "LondonNo1"

    def __init__(self, hello):
        self.cfg = hello.get("config") or {}
        self.me_id = hello.get("slot", 0)
        self.rng = random.Random(hello.get("seed", 1))
        cfg = self.cfg
        self.reach = (cfg.get("loot") or {}).get("pickup_radius", 4.0)
        self.cube_reach = (cfg.get("cubes") or {}).get("pickup_radius", 3.5)
        self.size_per_cube = (cfg.get("cubes") or {}).get("size_per_cube", 0.05)
        self.map = None
        if nav is not None:
            dirs = []
            try:
                dirs.append(hello["map"]["dir"])
            except Exception:
                pass
            here = os.path.dirname(os.path.abspath(__file__))
            dirs += ["/arena", os.path.join(here, "arena"), os.path.join(here, "..", "mechfight-kit", "arena")]
            for d in dirs:
                try:
                    if d and os.path.exists(os.path.join(d, "v2", "world.json")):
                        self.map = nav.Map(d)
                        log("map loaded from %s in %.2fs" % (d, self.map.build_s))
                        break
                except Exception as e:
                    log("map load failed", d, e)
        self.flow = None
        self.flow_key = None
        self.strafe_dir = 1
        self.strafe_until = 0.0
        self.pos_hist = []
        self.stuck_until = 0.0
        self.stuck_count = 0
        self.unstick_dir = None
        self.last_dir = None
        self.cover = None
        self.cover_t = -10.0
        self.last_damage_t = -100.0
        self.last_combat_t = -100.0
        self.last_damage_bearing = None
        self.unseen_bearing = None
        self.unseen_t = -100.0
        self.unseen_by = None
        self.attackers = {}       # id -> (t, bearing)
        self.vel_ema = {}         # id -> (t, vx, vy, vz)
        self.last_target_id = None
        self.retreating = False
        self.peek = None
        self.peek_t = -10.0
        self.scan_yaw = None
        self.scan_t = -10.0
        self.mode = ""
        self.blocked_pickups = {}
        self.t = 0.0

    # ------------------------------------------------------------------ helpers
    def los(self, a, b):
        if self.map is None:
            return True
        return self.map.los(a[0], a[1], a[2], b[0], b[1], b[2])

    def mech_center(self, m):
        s = 1 + self.size_per_cube * (m.get("cubes") or 0)
        return [m["pos"][0], m["pos"][1], m["pos"][2] + 4.5 * s]

    def walk_ok(self, x, y, dx, dy, dist):
        """Can we walk from (x,y) along unit (dx,dy) for `dist` meters without a tall step up?"""
        m = self.map
        if m is None:
            return True
        prev = m.surf_at(x, y)
        sx, sy = -dy * 1.8, dx * 1.8
        d = 2.0
        while d <= dist + 1e-6:
            px, py = x + dx * d, y + dy * d
            if not m.inside(px, py, 4.0):
                return False
            s = m.surf_at(px, py)
            if s - prev > 1.15:
                return False
            if m.surf_at(px + sx, py + sy) - prev > 1.6 or m.surf_at(px - sx, py - sy) - prev > 1.6:
                return False
            prev = s
            d += 2.0
        return True

    def steer(self, x, y, dx, dy, look=7.0):
        """Pick a walkable direction close to the desired one."""
        L = math.hypot(dx, dy)
        if L < 1e-6:
            return 0.0, 0.0
        dx, dy = dx / L, dy / L
        base = math.atan2(dy, dx)
        sgn = 1.0
        if self.last_dir is not None and wrap(self.last_dir - base) < 0:
            sgn = -1.0
        offs = [0.0]
        for o in (0.3, 0.6, 0.95, 1.3, 1.75, 2.2, 2.65):
            offs += [sgn * o, -sgn * o]
        offs.append(math.pi)
        for o in offs:
            a = base + o
            cx, cy = math.cos(a), math.sin(a)
            if self.walk_ok(x, y, cx, cy, look):
                self.last_dir = a
                return cx, cy
        for o in offs:
            a = base + o
            cx, cy = math.cos(a), math.sin(a)
            if self.walk_ok(x, y, cx, cy, 3.0):
                self.last_dir = a
                return cx, cy
        self.last_dir = None
        return dx, dy

    def zone_info(self, obs):
        z = obs.get("zone") or {}
        t = obs.get("t", 0.0)
        cx, cy = z.get("center", [0.0, 0.0])
        r = z.get("radius", 560.0)
        nx_, ny_ = z.get("next_center", [cx, cy])
        nr = z.get("next_radius", r)
        s0, s1 = z.get("shrink_start", t + 60.0), z.get("shrink_end", t + 110.0)
        if t >= s0 and s1 > s0:
            f = min(1.0, (t - s0) / (s1 - s0))
            cx, cy, r = cx + (nx_ - cx) * f, cy + (ny_ - cy) * f, r + (nr - r) * f
        return (cx, cy, r), (nx_, ny_, nr), s0, s1

    def update_flow(self, nxt):
        if self.map is None:
            return
        key = (round(nxt[0], 1), round(nxt[1], 1), round(nxt[2], 1))
        if key != self.flow_key:
            m = self.map
            rg = max(4.0, nxt[2] * 0.55)
            goals = []
            c0x, c0y = nxt[0], nxt[1]
            I0 = int((c0x - m.minx) / nav.CRES)
            J0 = int((c0y - m.miny) / nav.CRES)
            R = int(rg / nav.CRES) + 1
            for J in range(max(0, J0 - R), min(m.cny, J0 + R + 1)):
                for I in range(max(0, I0 - R), min(m.cnx, I0 + R + 1)):
                    c = J * m.cnx + I
                    px, py = m.ccenter(c)
                    if math.hypot(px - c0x, py - c0y) <= rg and not m.cmix[c]:
                        goals.append(c)
            if not goals:
                goals = [m.ccell(c0x, c0y)]
            self.flow = nav.FlowField(m, goals, key)
            self.flow_key = key

    def reachable_around(self, x, y, px, py, r):
        """Check if (px, py) is reachable directly or via a 1-corner detour around a building."""
        dx, dy = (px - x) / max(r, 1e-6), (py - y) / max(r, 1e-6)
        if self.walk_ok(x, y, dx, dy, r):
            return True, 0.0
        mx, my = 0.5 * (x + px), 0.5 * (y + py)
        nx_, ny_ = -dy, dx
        for off in (7.0, -7.0, 14.0, -14.0):
            wx, wy = mx + nx_ * off, my + ny_ * off
            d1 = math.hypot(wx - x, wy - y)
            d2 = math.hypot(px - wx, py - wy)
            if self.walk_ok(x, y, (wx - x) / d1, (wy - y) / d1, d1) and \
               self.walk_ok(wx, wy, (px - wx) / d2, (py - wy) / d2, d2):
                return True, abs(off) * 0.6
        return False, 0.0

    def find_cover(self, x, y, z, threats, cur, nxt, t_end=None):
        """A nearby walkable point hidden from every threat eye position (with fallback to primary threat)."""
        m = self.map
        if m is None or not threats:
            return None
        my_s = m.surf_at(x, y)
        best = None
        bests = 1e18
        if t_end is None:
            t_end = time.perf_counter() + 0.0038
        for r in (6.0, 11.0, 17.0, 24.0, 33.0, 44.0, 58.0):
            n_ang = 16 if r <= 24.0 else 24
            for k in range(n_ang):
                a = k * TAU / n_ang
                px, py = x + r * math.cos(a), y + r * math.sin(a)
                if not m.inside(px, py, 6.0):
                    continue
                fc = m.cell(px, py)
                if m.wall[fc]:
                    continue
                s = m.surf[fc]
                if abs(s - my_s) > 1.6:
                    continue
                if math.hypot(px - cur[0], py - cur[1]) > cur[2] - 12:
                    continue
                zpen = 0.0
                if nxt[2] > 0 and math.hypot(px - nxt[0], py - nxt[1]) > nxt[2] + 15:
                    zpen = 45.0
                hidden = True
                for idx_t, (ex, ey, ez, wgt) in enumerate(threats):
                    if m.los(ex, ey, ez, px, py, s + 4.5) or (idx_t == 0 and m.los(ex, ey, ez, px, py, s + 8.5)):
                        hidden = False
                        break
                if not hidden:
                    continue
                base_score = r + zpen
                if base_score >= bests:
                    continue
                ok, detour_pen = self.reachable_around(x, y, px, py, r)
                if not ok:
                    continue
                score = base_score + detour_pen
                if score < bests:
                    best = (px, py)
                    bests = score
                if time.perf_counter() > t_end:
                    return best
            if best is not None:
                return best
        if best is None and len(threats) > 1 and time.perf_counter() < t_end:
            return self.find_cover(x, y, z, threats[:1], cur, nxt, t_end=t_end)
        return best

    def find_peek(self, x, y, z, tc):
        """Nearest walkable point with line of sight from our muzzle height to the target center."""
        m = self.map
        if m is None:
            return None
        my_s = m.surf_at(x, y)
        t_end = time.perf_counter() + 0.0028
        for r in (4.0, 8.0, 13.0, 19.0, 26.0):
            best = None
            bd = 1e18
            for k in range(12):
                a = k * TAU / 12
                px, py = x + r * math.cos(a), y + r * math.sin(a)
                if not m.inside(px, py, 6.0):
                    continue
                s = m.surf_at(px, py)
                if s - my_s > 1.2:
                    continue
                dd = math.hypot(px - tc[0], py - tc[1])
                if dd >= bd:
                    continue
                if not self.walk_ok(x, y, math.cos(a), math.sin(a), r):
                    continue
                if m.los(px, py, s + 7.0, tc[0], tc[1], tc[2]):
                    best = (px, py)
                    bd = dd
            if best is not None or time.perf_counter() > t_end:
                return best
        return None

    def intercept(self, src, tgt, vel, speed, lead_mult=1.0):
        """Point to aim at so a straight projectile at `speed` meets a target moving at `vel`."""
        px, py, pz = tgt
        vx, vy, vz = vel[0] * lead_mult, vel[1] * lead_mult, vel[2] * 0.3 * lead_mult
        t = 0.0
        for _ in range(4):
            ax, ay, az = px + vx * t, py + vy * t, pz + vz * t
            d = math.sqrt((ax - src[0]) ** 2 + (ay - src[1]) ** 2 + (az - src[2]) ** 2)
            t = d / max(speed, 1.0)
        return [px + vx * t, py + vy * t, pz + vz * t], t

    @staticmethod
    def aim_to(src, p):
        dx, dy, dz = p[0] - src[0], p[1] - src[1], p[2] - src[2]
        return [math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))]

    # ------------------------------------------------------------------ main
    def act(self, obs):
        t_start = time.perf_counter()
        me = obs["self"]
        t = obs.get("t", 0.0)
        self.t = t
        x, y, z = me["pos"]
        st = me.get("stats") or {}
        hp, max_hp = me.get("hp", 1400.0), me.get("max_hp", 1400.0)
        hpf = hp / max(1.0, max_hp)
        act = {"tick": obs["tick"]}

        # ---- upgrades
        offer = obs.get("offer")
        if offer and offer.get("choices"):
            ids = [c.get("id", "") for c in offer["choices"]]
            best = min(range(len(ids)), key=lambda i: UPGRADE_PRIORITY.index(ids[i]) if ids[i] in UPGRADE_PRIORITY else 99)
            act["pick"] = best

        # ---- weapons
        raw_weapons = me.get("weapons") or []
        weapons = [raw_weapons[0] if len(raw_weapons) > 0 else None,
                   raw_weapons[1] if len(raw_weapons) > 1 else None]
        slot_of = {}
        for i, w in enumerate(weapons):
            if w and "kind" in w:
                slot_of[w["kind"]] = i
        muzzle = [x, y, z + st.get("muzzle_height", 7)]

        # ---- enemies & velocity smoothing
        enemies = []
        npcs = []
        vis_ids = set()
        for m in obs.get("mechs", []):
            if m.get("kind") == "mech":
                if m.get("id") == me.get("id"):
                    continue
                enemies.append(m)
                vis_ids.add(m["id"])
                mv = m.get("vel") or [0, 0, 0]
                prev = self.vel_ema.get(m["id"])
                if prev and t - prev[0] < 0.6:
                    a = 0.65
                    self.vel_ema[m["id"]] = (t, a * mv[0] + (1 - a) * prev[1], a * mv[1] + (1 - a) * prev[2], mv[2])
                else:
                    self.vel_ema[m["id"]] = (t, mv[0], mv[1], mv[2])
            else:
                npcs.append(m)

        # ---- events (track both damaged and hit for regen timer, plus unseen attackers)
        for e in obs.get("events", []):
            et = e.get("type")
            if et == "damaged":
                self.last_damage_t = t
                self.last_combat_t = t
                if e.get("by") is not None:
                    b = e.get("bearing")
                    self.last_damage_bearing = b
                    self.attackers[e["by"]] = (t, b)
                    if e["by"] not in vis_ids and b is not None:
                        self.unseen_bearing = b
                        self.unseen_t = t
                        self.unseen_by = e["by"]
            elif et == "hit":
                self.last_combat_t = t
            elif et == "take_failed":
                self.blocked_pickups[e.get("id")] = t

        if self.unseen_by in vis_ids:
            self.unseen_t = -100.0

        # ---- zone
        cur, nxt, s0, s1 = self.zone_info(obs)
        self.update_flow(nxt)
        d_cur = math.hypot(x - cur[0], y - cur[1])
        d_next = math.hypot(x - nxt[0], y - nxt[1])
        in_next_safe = d_next < max(0.0, nxt[2] - max(8.0, nxt[2] * 0.12))
        walk = st.get("walk_speed", 11.7)
        time_to_shrink = s0 - t
        urgent_zone = (not me.get("in_zone", True)) or d_cur > cur[2] - 10
        early_zone = not in_next_safe and (time_to_shrink < (d_next - nxt[2] * 0.75) / walk + 15 or t >= s0)

        # ---- evaluate visible enemies
        vis = []
        for m in enemies:
            c = self.mech_center(m)
            d = math.dist(c[:2], (x, y))
            has_los = self.los(muzzle, c)
            vis.append((m, c, d, has_los))

        # target choice: finish low-HP targets aggressively, prefer LoS and recent attackers
        target = None
        best = 1e18
        for (m, c, d, has_los) in vis:
            mhp = m.get("hp", 1400)
            sc = d + 0.14 * mhp
            if not has_los:
                sc += 450
            elif mhp < 180 and d < 225:
                sc -= 500
            elif mhp < 380 and d < 210:
                sc -= 260
            att = self.attackers.get(m["id"])
            if att and t - att[0] < 3.5:
                sc -= 45
            if m["id"] == self.last_target_id:
                sc -= 40
            if sc < best:
                best = sc
                target = (m, c, d, has_los)
        self.last_target_id = target[0]["id"] if target else None

        # build threats list from visible enemies (recent attackers and closest first)
        threats = []
        for (m, c, d, has_los) in sorted(
            vis,
            key=lambda v: (0 if (self.attackers.get(v[0]["id"]) and t - self.attackers[v[0]["id"]][0] < 6.0) else 1, v[2]),
        ):
            if d < 240:
                s = 1 + self.size_per_cube * (m.get("cubes") or 0)
                threats.append((m["pos"][0], m["pos"][1], m["pos"][2] + 8 * s, 1))

        # ---- aiming and firing
        fire = [False, False]
        aim = None
        gun = weapons[slot_of["gun"]] if "gun" in slot_of else None
        laser_i = slot_of.get("laser")
        light_i = slot_of.get("lightning")
        mortar_i = slot_of.get("mortar")
        my_aim = me.get("aim") or [0.0, 0.0]
        laser_charging = laser_i is not None and (weapons[laser_i].get("charge") or 0) > 0

        # Urgent turn to spot an unseen rear attacker (unless finishing a 1-shot kill or releasing laser)
        finishing_kill = target is not None and target[3] and target[0].get("hp", 1400) < 240 and target[2] < 210
        need_spot_unseen = (
            (t - self.unseen_t < 1.2)
            and (self.unseen_by not in vis_ids)
            and (self.unseen_bearing is not None)
            and not finishing_kill
            and not laser_charging
        )

        if need_spot_unseen:
            aim = [self.unseen_bearing, 0.0]
        elif target is not None:
            m, c, d, has_los = target
            ve = self.vel_ema.get(m["id"])
            tv = [ve[1], ve[2], ve[3]] if ve else (m.get("vel") or [0, 0, 0])
            gspeed = (gun.get("stats") or {}).get("speed", 252) if gun else 252
            lead_mult = 0.92 if d < 120 else 0.78
            p, tof = self.intercept(muzzle, c, tv, gspeed, lead_mult=lead_mult)
            aim_gun = self.aim_to(muzzle, p)
            aim_direct = self.aim_to(muzzle, c)
            s = 1 + self.size_per_cube * (m.get("cubes") or 0)
            tol = max(0.008, (2.15 * s) / max(d, 1.0))
            lstats = (weapons[laser_i].get("stats") or {}) if laser_i is not None else {}
            use_laser = laser_i is not None and has_los and d < lstats.get("range", 280) * 0.95
            laser_ch = (weapons[laser_i].get("charge") or 0) if laser_i is not None else 0.0
            laser_full = lstats.get("charge", 0.75) if laser_i is not None else 0.75
            gstats = (gun.get("stats") or {}) if gun else {}
            gun_active = (
                gun is not None
                and (gun.get("ammo") or 0) > 0
                and (gun.get("reload") or 0) <= 0
                and d < gstats.get("range", 230) * 0.98
            )
            # Keep aiming at aim_gun while laser builds charge so the gun keeps firing non-stop;
            # snap to aim_direct on the final 0.18s of laser charge to release the hitscan beam on target.
            if use_laser and (
                laser_ch >= laser_full - 0.18
                or (not gun_active and (laser_charging or (weapons[laser_i].get("cooldown") or 0) <= 0.15))
            ):
                aim = aim_direct
            else:
                aim = aim_gun
            yaw_err = abs(wrap(my_aim[0] - aim[0]))
            pit_err = abs(my_aim[1] - aim[1])
            # gun
            if gun and has_los and d < gstats.get("range", 230) * 0.98:
                ge_y = abs(wrap(my_aim[0] - aim_gun[0]))
                ge_p = abs(my_aim[1] - aim_gun[1])
                if (
                    ge_y < tol * 1.35
                    and ge_p < tol * 1.5 + 0.012
                    and (gun.get("ammo") or 0) > 0
                    and (gun.get("reload") or 0) <= 0
                ):
                    fire[slot_of["gun"]] = True
            # laser
            if laser_i is not None:
                lw = weapons[laser_i]
                full = lstats.get("charge", 0.75)
                ch = lw.get("charge") or 0
                if ch > 0:
                    le_y = abs(wrap(my_aim[0] - aim_direct[0]))
                    le_p = abs(my_aim[1] - aim_direct[1])
                    killable_now = m.get("hp", 1400) <= 110 and ch >= lstats.get("min_charge", 0.15)
                    ready_rel = use_laser and le_y < tol and le_p < tol + 0.01 and (ch >= full - 0.06 or killable_now)
                    if not ready_rel:
                        fire[laser_i] = True
                    if ch >= full + 1.65:
                        fire[laser_i] = False
                elif use_laser and (lw.get("cooldown") or 0) <= 0.05 and yaw_err < 0.55:
                    fire[laser_i] = True
            # lightning
            if light_i is not None:
                lw = weapons[light_i]
                lt_stats = lw.get("stats") or {}
                rng = lt_stats.get("range", 45)
                cone = math.radians(lt_stats.get("cone_deg", 30))
                if has_los and d < rng * 0.95 and abs(wrap(my_aim[0] - aim_direct[0])) < cone * 0.42 and me.get("energy", 0) > 8:
                    fire[light_i] = True
            # mortar (if carried)
            if mortar_i is not None and 25 < d < 210:
                mw = weapons[mortar_i]
                m_stats = mw.get("stats") or {}
                v = m_stats.get("speed", 72)
                g = m_stats.get("gravity", 22)
                h = c[2] - 4.5 * s * 0.8 - (z + st.get("height", 9) + 0.5)
                disc = v ** 4 - g * (g * d * d + 2 * h * v * v)
                if disc > 0 and (mw.get("cooldown") or 0) <= 0:
                    th = math.atan((v * v - math.sqrt(disc)) / (g * d)) if has_los else math.atan((v * v + math.sqrt(disc)) / (g * d))
                    if -0.6 < th < 1.25 and not laser_charging and not fire[slot_of.get("gun", 0)]:
                        flight = d / max(v * math.cos(th), 1.0)
                        lead = [c[0] + tv[0] * flight, c[1] + tv[1] * flight]
                        yaw_m = math.atan2(lead[1] - y, lead[0] - x)
                        if abs(wrap(my_aim[0] - yaw_m)) < 0.03 and abs(my_aim[1] - th) < 0.03:
                            fire[mortar_i] = True
                        elif not has_los:
                            aim = [yaw_m, th]
        else:
            if laser_charging:
                ch = weapons[laser_i].get("charge") or 0
                full = (weapons[laser_i].get("stats") or {}).get("charge", 0.75)
                fire[laser_i] = ch < full + 1.0
            # Only shoot NPCs/crates when healthy (or already in combat) so we don't reset the 5s HP regen timer
            sec = None
            can_farm_shoot = hpf > 0.92 or (hpf > 0.65 and t - self.last_combat_t < 1.5)
            if can_farm_shoot:
                bestd = 1e18
                for n in npcs:
                    c = [n["pos"][0], n["pos"][1], n["pos"][2] + 1.75]
                    d = math.dist(c, muzzle)
                    if d < 105 and d < bestd and self.los(muzzle, c):
                        bestd = d
                        sec = (c, n.get("vel") or [0, 0, 0], d)
                if sec is None and not urgent_zone:
                    for cr in obs.get("crates", []):
                        c = [cr["pos"][0], cr["pos"][1], cr["pos"][2] + 1.2]
                        d = math.dist(c, muzzle)
                        if d < 75 and d < bestd and self.los(muzzle, c):
                            bestd = d
                            sec = (c, [0, 0, 0], d)
            if sec is not None and gun:
                c, v, d = sec
                gspeed = (gun.get("stats") or {}).get("speed", 252)
                p, _ = self.intercept(muzzle, c, v, gspeed)
                aim = self.aim_to(muzzle, p)
                tol = max(0.008, 1.35 / max(d, 1))
                if abs(wrap(my_aim[0] - aim[0])) < tol and abs(my_aim[1] - aim[1]) < tol + 0.01 and (gun.get("ammo") or 0) > 0:
                    fire[slot_of["gun"]] = True
            elif t - self.last_damage_t < 4.5 and self.last_damage_bearing is not None:
                aim = [self.last_damage_bearing, 0.0]
            else:
                heard = None
                for snd in obs.get("sounds", []):
                    if snd.get("kind") in ("gun", "laser", "laser_charge", "lightning", "step", "boost", "jet", "mortar", "reload"):
                        if heard is None or snd.get("dist") == "near":
                            heard = snd
                if heard is not None and "bearing" in heard:
                    self.scan_yaw = heard["bearing"]
                    self.scan_t = t
                if self.scan_yaw is not None and t - self.scan_t < 2.5:
                    aim = [self.scan_yaw, 0.0]

        # reload when empty or idle
        if gun and (gun.get("reload") or 0) <= 0:
            ammo = gun.get("ammo")
            mag = (gun.get("stats") or {}).get("mag", 12)
            if ammo is not None and (ammo == 0 or (target is None and ammo < mag * 0.5)):
                act["reload"] = True

        # ---- movement & looting
        move = None
        boost = False
        jump = False
        self.mode = ""
        threat_near = target is not None and target[3] and target[2] < 195

        # Always grab weapon pickup if already within reach
        for pk in obs.get("pickups", []):
            knd = pk.get("kind")
            if knd in slot_of or WEAPON_VALUE.get(knd, 0) <= 0:
                continue
            if t - self.blocked_pickups.get(pk.get("id"), -100) < 3:
                continue
            cur_val = max([WEAPON_VALUE.get(w.get("kind"), 0) for i, w in enumerate(weapons) if w and i == 1] or [-1])
            if WEAPON_VALUE[knd] <= cur_val:
                continue
            if math.dist(pk["pos"][:2], (x, y)) < self.reach - 0.25 and abs(pk["pos"][2] - z) < 3.8:
                if not (weapons[1] and weapons[1].get("kind") == "laser" and (weapons[1].get("charge") or 0) > 0):
                    act["take"] = pk["id"]
                    if weapons[1] is not None:
                        act["drop"] = 1
                break

        want = None
        if not threat_near:
            bestd = 1e18
            for pk in obs.get("pickups", []):
                knd = pk.get("kind")
                if knd in slot_of or WEAPON_VALUE.get(knd, 0) <= 0:
                    continue
                if t - self.blocked_pickups.get(pk.get("id"), -100) < 3:
                    continue
                cur_val = max([WEAPON_VALUE.get(w.get("kind"), 0) for i, w in enumerate(weapons) if w and i == 1] or [-1])
                if WEAPON_VALUE[knd] <= cur_val:
                    continue
                d = math.dist(pk["pos"][:2], (x, y))
                if abs(pk["pos"][2] - z) > 3.5 and d < 6:
                    continue
                if d < 120 and d < bestd and (math.hypot(pk["pos"][0] - cur[0], pk["pos"][1] - cur[1]) < cur[2] - 15):
                    bestd = d
                    want = pk

        # Supply drops (full HP + energy + 3 cubes)
        drop_goal = None
        if not threat_near or hpf < 0.75:
            bestd = 1e18
            for dr in obs.get("drops", []):
                if dr.get("holder") not in (None, me.get("id")):
                    continue
                if (dr.get("lands_in") or 0) > 12.0:
                    continue
                dp = dr["pos"]
                d = math.dist(dp[:2], (x, y))
                if d < 140 and d < bestd and math.hypot(dp[0] - cur[0], dp[1] - cur[1]) < cur[2] - 15:
                    bestd = d
                    drop_goal = dp

        cube_goal = None
        if not threat_near:
            bestd = 1e18
            for cb in obs.get("cubes", []):
                d = math.dist(cb["pos"][:2], (x, y))
                if d < 95 and d < bestd and abs(cb["pos"][2] - z) < 4:
                    bestd = d
                    cube_goal = cb["pos"]

        recently_hit = t - self.last_damage_t < 3.0
        n_los = len([v for v in vis if v[3] and v[2] < 185]) + (1 if need_spot_unseen else 0)
        if not finishing_kill and (hpf < RETREAT_HP or (n_los >= 2 and hpf < 0.75) or n_los >= 3):
            self.retreating = True
        elif finishing_kill or hpf > RECOVER_HP or (target is None and hpf > 0.62 and not recently_hit and t - self.last_damage_t > 10):
            self.retreating = False
        retreat = self.retreating and (target is not None or recently_hit or hpf < RECOVER_HP)

        # Hold fire only when actually hidden from all enemies and waiting for the 5s regen timer
        if retreat and n_los == 0 and t - self.last_damage_t > 1.2:
            if not laser_charging:
                fire = [False, False]

        if urgent_zone or (early_zone and not threat_near and not retreat):
            self.mode = "zone"
            dirv = (
                self.flow.direction(x, y)
                if (self.flow is not None and self.map is not None and self.flow.dist[self.map.ccell(x, y)] < 1e17)
                else None
            )
            if dirv is None:
                dirv = (nxt[0] - x, nxt[1] - y)
            move = list(dirv)
            boost = (not me.get("in_zone", True)) or d_cur > cur[2] - 20 or (t >= s0 and not in_next_safe)
        elif retreat or (target is None and recently_hit and hpf < 0.65):
            self.mode = "cover"
            if t - self.cover_t > 0.45 or self.cover is None:
                self.cover = self.find_cover(x, y, z, threats, cur, nxt)
                self.cover_t = t
            if self.cover is not None:
                move = [self.cover[0] - x, self.cover[1] - y]
                if math.hypot(*move) < 1.5:
                    move = [0.0, 0.0]
                boost = me.get("energy", 0) > 22 and math.hypot(*move) > 4.0
            elif target is not None:
                # No nearby building cover: pull toward safe zone center while strafing
                m, c, d, _ = target
                ax_, ay_ = (x - c[0]) / max(d, 1), (y - c[1]) / max(d, 1)
                zx, zy = nxt[0] - x, nxt[1] - y
                zd = math.hypot(zx, zy) or 1.0
                z_pull = 0.7 if d_next > nxt[2] * 0.65 else 0.25
                move = [
                    ax_ * 0.6 - ay_ * self.strafe_dir * 0.8 + (zx / zd) * z_pull,
                    ay_ * 0.6 + ax_ * self.strafe_dir * 0.8 + (zy / zd) * z_pull,
                ]
                boost = me.get("energy", 0) > 25
        elif target is not None:
            self.mode = "fight"
            m, c, d, has_los = target
            if t > self.strafe_until:
                self.strafe_dir = -self.strafe_dir if self.rng.random() < 0.7 else self.strafe_dir
                self.strafe_until = t + self.rng.uniform(0.55, 1.45)
            ux, uy = (c[0] - x) / max(d, 1), (c[1] - y) / max(d, 1)
            if light_i is not None and hpf > 0.6:
                lo, hi = 15, 36
            elif laser_i is not None:
                lo, hi = 70, 170
            else:
                lo, hi = 48, 120
            gun_ready = gun is not None and (gun.get("ammo") or 0) > 0 and (gun.get("reload") or 0) <= 0
            laser_ready = (
                laser_i is not None
                and (weapons[laser_i].get("charge") or 0) >= (weapons[laser_i].get("stats") or {}).get("charge", 0.75) - 0.1
            )
            light_ready = light_i is not None and d < 45 and me.get("energy", 0) > 10
            armed = gun_ready or laser_ready or light_ready
            if not armed and has_los and d > 38 and not finishing_kill:
                self.mode = "duck"
                if t - self.cover_t > 0.45 or self.cover is None:
                    self.cover = self.find_cover(x, y, z, threats, cur, nxt)
                    self.cover_t = t
                if self.cover is not None:
                    move = [self.cover[0] - x, self.cover[1] - y]
                    if math.hypot(*move) < 1.5:
                        move = [0.0, 0.0]
                else:
                    move = [-uy * self.strafe_dir - ux * 0.3, ux * self.strafe_dir - uy * 0.3]
                boost = me.get("energy", 0) > 28
            elif not has_los:
                self.mode = "peek"
                if armed or hpf > 0.68:
                    if t - self.peek_t > 0.45:
                        self.peek = self.find_peek(x, y, z, c)
                        self.peek_t = t
                    if self.peek is not None:
                        move = [self.peek[0] - x, self.peek[1] - y]
                    else:
                        move = [ux * 0.6, uy * 0.6]
                else:
                    move = [0.0, 0.0]
            else:
                radial = 0.0
                if d > hi:
                    radial = 1.0
                elif d < lo:
                    radial = -1.0
                px_, py_ = -uy * self.strafe_dir, ux * self.strafe_dir
                move = [ux * radial + px_ * 0.9, uy * radial + py_ * 0.9]
                boost = me.get("energy", 0) > 48 and d < 130
        elif drop_goal is not None:
            self.mode = "drop"
            move = [drop_goal[0] - x, drop_goal[1] - y]
            if math.hypot(*move) < 2.5:
                move = [0.0, 0.0]
        elif want is not None:
            self.mode = "loot"
            move = [want["pos"][0] - x, want["pos"][1] - y]
            if math.hypot(*move) < 1.0:
                move = [0.0, 0.0]
        elif cube_goal is not None:
            self.mode = "cube"
            move = [cube_goal[0] - x, cube_goal[1] - y]
        else:
            self.mode = "roam"
            if not in_next_safe or d_next > nxt[2] * 0.58:
                dirv = (
                    self.flow.direction(x, y)
                    if (self.flow is not None and self.map is not None and self.flow.dist[self.map.ccell(x, y)] < 1e17)
                    else None
                )
                if dirv is None:
                    dirv = (nxt[0] - x, nxt[1] - y)
                move = [dirv[0] * 0.8, dirv[1] * 0.8]
            else:
                move = [0.0, 0.0]
            if hpf < 0.95 and not recently_hit:
                move = [move[0] * 0.35, move[1] * 0.35]

        # shells: step away from predicted mortar impact points
        mortar_cfg = ((self.cfg.get("weapons") or {}).get("mortar") or {})
        for pr in obs.get("projectiles", []):
            if pr.get("kind") != "mortar":
                continue
            px, py, pz = pr["pos"]
            vx, vy, vz = pr["vel"]
            g = mortar_cfg.get("gravity", 22)
            a, b_, cc = -0.5 * g, vz, pz - z
            disc = b_ * b_ - 4 * a * cc
            if disc < 0:
                continue
            tt = (-b_ - math.sqrt(disc)) / (2 * a)
            if tt <= 0:
                continue
            ix, iy = px + vx * tt, py + vy * tt
            dd = math.hypot(ix - x, iy - y)
            rad = mortar_cfg.get("splash_radius", 7) + 4.5
            if dd < rad and tt < 3.0:
                awx, awy = (x - ix) / max(dd, 0.5), (y - iy) / max(dd, 0.5)
                move = [awx, awy]
                boost = True

        if move is None:
            move = [0.0, 0.0]
        mlen = math.hypot(*move)
        if mlen > 1e-6:
            scale = min(1.0, mlen)
            sx, sy = self.steer(x, y, move[0], move[1])
            move = [sx * scale, sy * scale]

        # stuck detection: push away from adjacent walls, and jump/jet only if repeatedly trapped
        self.pos_hist.append((t, x, y, mlen > 0.35))
        while self.pos_hist and t - self.pos_hist[0][0] > 1.5:
            self.pos_hist.pop(0)
        if t < self.stuck_until and self.unstick_dir is not None:
            move = list(self.unstick_dir)
            jump = bool(me.get("grounded", True))
            boost = me.get("energy", 0) > 20 and (me.get("grounded", True) or self.stuck_count >= 2)
        elif len(self.pos_hist) > 9 and all(h[3] for h in self.pos_hist):
            h0 = self.pos_hist[0]
            if math.hypot(x - h0[1], y - h0[2]) < 1.5 and t - h0[0] > 1.1:
                self.stuck_count += 1
                self.stuck_until = t + 0.9
                rx, ry = 0.0, 0.0
                if self.map is not None:
                    my_s = self.map.surf_at(x, y)
                    for k in range(16):
                        a = k * TAU / 16
                        for rr in (2.2, 3.2):
                            if self.map.surf_at(x + rr * math.cos(a), y + rr * math.sin(a)) - my_s > 1.1:
                                rx -= math.cos(a) / rr
                                ry -= math.sin(a) / rr
                if math.hypot(rx, ry) > 0.1 and self.stuck_count <= 2:
                    rl = math.hypot(rx, ry)
                    tx, ty = rx / rl, ry / rl
                    self.unstick_dir = (tx * 0.85 - ty * 0.5, ty * 0.85 + tx * 0.5)
                else:
                    base_a = math.atan2(move[1], move[0]) if mlen > 0.1 else self.rng.uniform(0, TAU)
                    picked = None
                    for da in (1.5, -1.5, 2.3, -2.3, math.pi, 0.9, -0.9):
                        a = base_a + da
                        if self.walk_ok(x, y, math.cos(a), math.sin(a), 6.0):
                            picked = (math.cos(a), math.sin(a))
                            break
                    if picked is None:
                        a = self.rng.uniform(0, TAU)
                        picked = (math.cos(a), math.sin(a))
                    self.unstick_dir = picked
                self.pos_hist = []
        else:
            if len(self.pos_hist) > 6 and math.hypot(x - self.pos_hist[0][1], y - self.pos_hist[0][2]) > 4.0:
                self.stuck_count = 0

        if boost and me.get("energy", 0) < 14:
            boost = False

        if aim is None:
            aim = [my_aim[0] + 0.65, 0.0]
        pmin, pmax = st.get("min_pitch", -0.7), st.get("max_pitch", 1.3)
        aim = [wrap(aim[0]), max(pmin, min(pmax, aim[1]))]
        mv_out = [round(move[0], 3), round(move[1], 3)]
        mv_l = math.hypot(*mv_out)
        if mv_l > 1.0:
            mv_out = [mv_out[0] / mv_l, mv_out[1] / mv_l]
        act.update(
            move=mv_out,
            aim=[round(aim[0], 4), round(aim[1], 4)],
            fire=fire,
            boost=bool(boost),
            jump=bool(jump),
        )
        if self.flow is not None and not self.flow.done:
            left = BUDGET_S - (time.perf_counter() - t_start)
            if left > 0.0015:
                self.flow.step(min(left, 0.0025))
        mf.draw.label("%s hp%.0f %s" % (self.mode, hp, "T%d" % target[0]["id"] if target else ""))
        return act


if __name__ == "__main__":
    mf.run(Bot)
