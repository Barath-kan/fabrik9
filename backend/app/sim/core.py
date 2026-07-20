"""Simulation core — a faithful Python port of the browser's sim.js.

Same architecture: seeded world generation, utility task auction
(greedy contract-net), agent FSMs with congestion resolution,
belt/miner/assembler economy, and fault injection + recovery.

Determinism: all randomness flows through two seeded LCG streams using
exact integer arithmetic, so a seed reproduces a run bit-for-bit — a
property the test suite asserts. Tie-breaking in A* and task sorting is
insertion-ordered, so no hidden nondeterminism enters through containers.

New in the backend port: `to_dict()` / `from_dict()` snapshot support.
Snapshots capture a *consistent* world state: in-flight construction is
rolled back (blueprint refunded, patch un-reserved) like an uncommitted
transaction, and all agents resume from PLAN.
"""

from . import pathfinding
from .. import config as C
from ..config import T
from ..logging_setup import get_logger

log = get_logger("sim")

MOD = 2147483647
MUL = 16807


class Agent:
    __slots__ = ("id", "x", "y", "facing", "state", "task", "path", "dest",
                 "cargo", "timer", "wait_count", "build_queue", "build_idx",
                 "plan", "last_decision", "decision_pick", "idle_reason",
                 "tasks_done", "taboo_key", "taboo_until")

    def __init__(self, i, x, y):
        self.id = i
        self.x, self.y = x, y
        self.facing = (0, 1)
        self.state = "PLAN"
        self.task = None
        self.path = []
        self.dest = None
        self.cargo = 0
        self.timer = 0
        self.wait_count = 0
        self.build_queue = []
        self.build_idx = 0
        self.plan = None
        self.last_decision = []
        self.decision_pick = None
        self.idle_reason = None
        self.tasks_done = 0
        self.taboo_key = None
        self.taboo_until = 0


class Simulation:

    # ---------------- construction ----------------

    def __init__(self, seed=1337, _skip_world=False):
        self.seed = seed
        self.sim_seed = seed * 7 + 13          # runtime event stream
        self.stats = {"astar_runs": 0, "nodes": 0, "heap_ops": 0,
                      "path_sum": 0, "path_n": 0, "tasks_done": 0,
                      "faults_fixed": 0, "mttr_sum": 0, "fault_ticks": 0}
        self.tick = 0
        self.chaos_on = False
        self.structure_version = 1
        self.log = []
        self.log_seq = 0
        self.reserved = {}
        self.build_lock = -1
        self.stock = {"gears": 0, "ore_delivered": 0}
        self.gear_history = []
        self.assemblers = []
        self.miners = []
        self.belt_items = []
        self.lines = []
        self.faults = []
        self.occupied = set()
        if _skip_world:
            return

        s = seed

        def rnd():
            nonlocal s
            s = (s * MUL) % MOD
            return s / MOD

        self.grid = [[{"type": T.EMPTY, "dir": 0, "amount": 0}
                      for _ in range(C.COLS)] for _ in range(C.ROWS)]

        hqx, hqy = C.HQ
        centers = []
        guard = 0
        while len(centers) < 5 and guard < 500:
            guard += 1
            x = 4 + int(rnd() * (C.COLS - 8))
            y = 3 + int(rnd() * (C.ROWS - 6))
            if abs(x - hqx) + abs(y - hqy) < 9:
                continue
            if any(abs(cx - x) + abs(cy - y) < 9 for cx, cy in centers):
                continue
            centers.append((x, y))

        for _ in range(46):
            x = 2 + int(rnd() * (C.COLS - 4))
            y = 2 + int(rnd() * (C.ROWS - 4))
            if abs(x - hqx) < 4 and abs(y - hqy) < 4:
                continue
            if any(abs(cx - x) <= 2 and abs(cy - y) <= 2 for cx, cy in centers):
                continue
            self.grid[y][x]["type"] = T.ROCK
            if rnd() > 0.5 and x + 1 < C.COLS - 1:
                self.grid[y][x + 1]["type"] = T.ROCK

        self.patches = []
        for sx, sy in centers:
            tiles = []
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    x, y = sx + dx, sy + dy
                    if x < 1 or y < 1 or x >= C.COLS - 1 or y >= C.ROWS - 1:
                        continue
                    if abs(dx) + abs(dy) < 2 or rnd() > 0.35:
                        self.grid[y][x]["type"] = T.ORE
                        self.grid[y][x]["amount"] = C.ORE_PER_TILE
                        tiles.append((x, y))
            self.patches.append({"tiles": tiles, "automated": False, "no_auto": False})

        self.place_assembler(hqx, hqy)

        starts = [(hqx + 1, hqy + 2), (hqx - 1, hqy + 2), (hqx, hqy - 2)]
        self.agents = [Agent(i, *starts[i]) for i in range(C.N_AGENTS)]

        self.add_log(f"World seed {seed} — squad of {C.N_AGENTS} agents online.", "")
        log.info("agents spawned", extra={"seed": seed, "agents": C.N_AGENTS})

    # ---------------- helpers ----------------

    def sim_rnd(self):
        self.sim_seed = (self.sim_seed * MUL) % MOD
        return self.sim_seed / MOD

    def in_bounds(self, x, y):
        return 0 <= x < C.COLS and 0 <= y < C.ROWS

    def tile_at(self, x, y):
        return self.grid[y][x]

    def agent_at(self, x, y, excl=None):
        for a in self.agents:
            if a is not excl and a.x == x and a.y == y:
                return a
        return None

    @staticmethod
    def dist(ax, ay, bx, by):
        return abs(ax - bx) + abs(ay - by)

    def base_cost(self, x, y):
        t = self.grid[y][x]["type"]
        if t == T.EMPTY or t == T.ORE:
            return 1
        if t == T.BELT:
            return 3
        return float("inf")

    def add_log(self, msg, cls=""):
        self.log.append({"t": self.tick, "msg": msg, "cls": cls})
        if len(self.log) > 70:
            self.log.pop(0)
        self.log_seq += 1

    def place_assembler(self, x, y):
        self.grid[y][x]["type"] = T.ASM
        self.assemblers.append({"x": x, "y": y, "buffer": 0, "progress": 0, "crafted": 0})
        self.structure_version += 1

    def nearest_assembler(self, x, y):
        best, bd = None, float("inf")
        for a in self.assemblers:
            d = abs(a["x"] - x) + abs(a["y"] - y)
            if d < bd:
                bd, best = d, a
        return best, bd

    # ---------------- routing ----------------

    def route(self, ag, dest):
        dx, dy, adjacent = dest
        if adjacent:
            return pathfinding.astar(
                self, ag.x, ag.y,
                lambda x, y: abs(x - dx) + abs(y - dy) == 1
                             and self.base_cost(x, y) != float("inf")
                             and self.agent_at(x, y, ag) is None,
                (dx, dy), ag)
        return pathfinding.astar(self, ag.x, ag.y,
                                 lambda x, y: x == dx and y == dy, (dx, dy), ag)

    def assign_route(self, ag, dest, note=None, cls="ev-path"):
        p = self.route(ag, dest)
        if p is None:
            return False
        ag.dest = dest
        ag.path = p
        ag.wait_count = 0
        ag.state = "MOVE"
        if not p:
            self.arrive(ag)
        if note:
            self.add_log(f"A{ag.id + 1}: {note} — A* {len(p)} tiles", cls)
        return True

    # ---------------- utility task auction ----------------

    @staticmethod
    def r_key(t):
        k = t["type"]
        if k == "REPAIR":
            f = t["fault"]
            return f"repair:{f['x']},{f['y']}"
        if k == "MINE":
            x, y = t["tile"]
            return f"mine:{x},{y}"
        if k == "BUILD":
            return "build"
        return None

    def release_reservations(self, ag):
        self.reserved = {k: v for k, v in self.reserved.items() if v != ag.id}
        if self.build_lock == ag.id:
            self.build_lock = -1

    def candidate_tasks(self, ag):
        c = []

        def taboo(k):
            return k is not None and k == ag.taboo_key and self.tick < ag.taboo_until

        if ag.cargo > 0:
            best, bd = None, float("inf")
            for a in self.assemblers:
                d = self.dist(ag.x, ag.y, a["x"], a["y"])
                if d < bd:
                    bd, best = d, a
            score = 999 if ag.cargo >= C.CARGO_CAP else 22 + ag.cargo * 3 - bd * 0.5
            c.append({"type": "DELIVER", "asm": best, "score": score,
                      "label": f"DELIVER x{ag.cargo}"})

        for f in self.faults:
            k = f"repair:{f['x']},{f['y']}"
            if k in self.reserved or taboo(k):
                continue
            c.append({"type": "REPAIR", "fault": f,
                      "score": 100 - self.dist(ag.x, ag.y, f["x"], f["y"]) * 0.5,
                      "label": f"REPAIR ({f['x']},{f['y']})"})

        if self.build_lock == -1 and "build" not in self.reserved:
            plan = self.next_automation_plan(ag)
            if plan and self.stock["gears"] >= plan["cost"]:
                fx, fy = plan["steps"][0]["x"], plan["steps"][0]["y"]
                c.append({"type": "BUILD", "plan": plan,
                          "score": 70 - self.dist(ag.x, ag.y, fx, fy) * 0.3,
                          "label": f"BUILD LINE ({plan['cost']}g)"})

        if ag.cargo < C.CARGO_CAP:
            best, bd = None, float("inf")
            for p in self.patches:
                if p["automated"]:
                    continue
                for (tx, ty) in p["tiles"]:
                    g = self.grid[ty][tx]
                    if g["type"] != T.ORE or g["amount"] <= 0:
                        continue
                    k = f"mine:{tx},{ty}"
                    if k in self.reserved or taboo(k):
                        continue
                    d = self.dist(ag.x, ag.y, tx, ty)
                    if d < bd:
                        bd, best = d, (tx, ty)
            if best:
                c.append({"type": "MINE", "tile": best,
                          "score": 40 - bd * 0.5,
                          "label": f"MINE ({best[0]},{best[1]})"})

        c.append({"type": "SUPERVISE",
                  "asm": self.assemblers[ag.id % len(self.assemblers)],
                  "score": 1, "label": "SUPERVISE"})
        return c

    def run_auction(self, ag):
        cands = sorted(self.candidate_tasks(ag), key=lambda t: -t["score"])
        ag.last_decision = [{"label": t["label"], "score": t["score"]}
                            for t in cands[:4]]
        log.debug("auction start", extra={"agent": ag.id + 1, "tick": self.tick,
                                          "candidates": len(cands)})
        for i, t in enumerate(cands):
            if self.try_assign(ag, t):
                # Record-only explainability metadata: which candidate won,
                # why, and who it beat. No RNG, no control flow — the auction
                # itself is unchanged (determinism fingerprint depends on it).
                ag.decision_pick = {
                    "label": t["label"], "type": t["type"],
                    "score": round(t["score"], 1), "rank": i, "tick": self.tick,
                    "reason": ("highest utility score" if i == 0 else
                               f"{i} higher-scored candidate(s) unroutable"),
                    "rejected": [{"label": c["label"],
                                  "score": round(c["score"], 1),
                                  "why": "no viable route"}
                                 for c in cands[:i]],
                }
                if t["type"] == "SUPERVISE":
                    ag.idle_reason = (
                        "no pending tasks — factory automated; supervising"
                        if len(cands) == 1 else
                        "pending tasks unreachable — standing by")
                else:
                    ag.idle_reason = None
                log.debug("auction resolved",
                          extra={"agent": ag.id + 1, "tick": self.tick,
                                 "task": t["type"], "score": round(t["score"], 1)})
                return
        ag.state = "IDLE"
        ag.timer = 30
        ag.decision_pick = None
        ag.idle_reason = "no assignable tasks — all candidates reserved or unroutable"
        log.debug("auction idle", extra={"agent": ag.id + 1, "tick": self.tick})

    def try_assign(self, ag, t):
        k = t["type"]
        if k == "DELIVER":
            asm = t["asm"]
            idx = self.assemblers.index(asm) + 1
            if not self.assign_route(ag, (asm["x"], asm["y"], True),
                                     f"hauling {ag.cargo} ore to Assembler-{idx}"):
                return False
            ag.task = t
            return True

        if k == "REPAIR":
            f = t["fault"]
            if not self.assign_route(ag, (f["x"], f["y"], False),
                                     f"dispatched to belt fault at ({f['x']},{f['y']})", "ev-fix"):
                return False
            self.reserved[self.r_key(t)] = ag.id
            ag.task = t
            return True

        if k == "BUILD":
            plan = t["plan"]
            self.stock["gears"] -= plan["cost"]
            self.build_lock = ag.id
            plan["patch"]["automated"] = True
            ag.build_queue = plan["steps"]
            ag.build_idx = 0
            ag.plan = plan
            ag.task = t
            self.add_log(f"A{ag.id + 1}: blueprint approved — "
                         f"{len(plan['steps'])} placements, {plan['cost']} gears", "ev-build")
            self.step_build_queue(ag)
            return True

        if k == "MINE":
            tx, ty = t["tile"]
            if not self.assign_route(ag, (tx, ty, False),
                                     f"mining ore at ({tx},{ty})"):
                return False
            self.reserved[self.r_key(t)] = ag.id
            ag.task = t
            return True

        if k == "SUPERVISE":
            asm = t["asm"]
            if self.dist(ag.x, ag.y, asm["x"], asm["y"]) <= 2:
                ag.task = t
                ag.state = "IDLE"
                ag.timer = 15
                return True
            if not self.assign_route(ag, (asm["x"], asm["y"], True)):
                ag.task = t
                ag.state = "IDLE"
                ag.timer = 15
                return True
            ag.task = t
            return True
        return False

    # ---------------- blueprint generation ----------------

    def next_automation_plan(self, ag):
        target, bd = None, float("inf")
        for p in self.patches:
            if p["automated"] or p["no_auto"] or not p["tiles"]:
                continue
            cx, cy = p["tiles"][0]
            d = abs(cx - ag.x) + abs(cy - ag.y)
            if d < bd:
                bd, target = d, p
        if target is None:
            return None

        alive = [(x, y) for (x, y) in target["tiles"]
                 if self.grid[y][x]["type"] == T.ORE]
        if not alive:
            target["no_auto"] = True
            return None
        miner_tile = min(alive, key=lambda t: self.nearest_assembler(t[0], t[1])[1])
        asm, ad = self.nearest_assembler(*miner_tile)
        asm_pos = (asm["x"], asm["y"])

        plan = {"patch": target, "steps": [], "cost": C.COST["MINER"],
                "line_belts": []}

        if ad > 15:
            spot = self.find_assembler_spot(*miner_tile)
            if spot:
                plan["steps"].append({"x": spot[0], "y": spot[1], "kind": T.ASM})
                plan["cost"] += C.COST["ASM"]
                asm_pos = spot

        ax, ay = asm_pos

        def empty_only(x, y):
            return 1 if self.grid[y][x]["type"] == T.EMPTY else float("inf")

        route = pathfinding.astar(
            self, miner_tile[0], miner_tile[1],
            lambda x, y: abs(x - ax) + abs(y - ay) == 1
                         and self.grid[y][x]["type"] == T.EMPTY,
            asm_pos, None, empty_only)
        if not route:
            target["no_auto"] = True
            self.add_log(f"Patch near ({miner_tile[0]},{miner_tile[1]}) "
                         f"unroutable — hand-mining only", "ev-warn")
            return None

        for i, (cx, cy) in enumerate(route):
            nx, ny = route[i + 1] if i + 1 < len(route) else asm_pos
            step = {"x": cx, "y": cy, "kind": T.BELT,
                    "dir": self.dir_index(nx - cx, ny - cy)}
            plan["steps"].append(step)
            plan["line_belts"].append(step)
        plan["cost"] += len(route) * C.COST["BELT"]
        plan["steps"].append({"x": miner_tile[0], "y": miner_tile[1],
                              "kind": T.MINER,
                              "out_x": route[0][0], "out_y": route[0][1],
                              "patch": target})
        return plan

    @staticmethod
    def dir_index(dx, dy):
        sx = (dx > 0) - (dx < 0)
        sy = (dy > 0) - (dy < 0)
        return C.DIRS.index((sx, sy))

    def find_assembler_spot(self, nx, ny):
        for r in range(3, 8):
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if abs(dx) + abs(dy) != r:
                        continue
                    x, y = nx + dx, ny + dy
                    if not (1 <= x < C.COLS - 1 and 1 <= y < C.ROWS - 1):
                        continue
                    if self.grid[y][x]["type"] != T.EMPTY:
                        continue
                    if all(self.in_bounds(x + a, y + b)
                           and self.grid[y + b][x + a]["type"] == T.EMPTY
                           for a, b in C.DIRS):
                        return (x, y)
        return None

    # ---------------- agent FSM ----------------

    def step_build_queue(self, ag):
        if ag.build_idx >= len(ag.build_queue):
            self.lines.append({"belts": ag.plan["line_belts"]})
            self.add_log(f"A{ag.id + 1}: automation line online.", "ev-build")
            ag.tasks_done += 1
            self.stats["tasks_done"] += 1
            self.build_lock = -1
            ag.plan = None
            ag.task = None
            ag.state = "PLAN"
            return
        s = ag.build_queue[ag.build_idx]
        adjacent = s["kind"] == T.ASM
        if not self.assign_route(ag, (s["x"], s["y"], adjacent)):
            self.add_log(f"A{ag.id + 1}: site ({s['x']},{s['y']}) unreachable — skipping",
                         "ev-warn")
            ag.build_idx += 1
            self.step_build_queue(ag)

    def place_structure(self, ag, s):
        t = self.grid[s["y"]][s["x"]]
        if s["kind"] == T.BELT:
            t["type"] = T.BELT
            t["dir"] = s["dir"]
        elif s["kind"] == T.ASM:
            self.place_assembler(s["x"], s["y"])
            self.add_log(f"A{ag.id + 1}: Assembler-{len(self.assemblers)} constructed",
                         "ev-build")
        elif s["kind"] == T.MINER:
            t["type"] = T.MINER
            self.miners.append({"x": s["x"], "y": s["y"],
                                "out_x": s["out_x"], "out_y": s["out_y"],
                                "cd": C.MINER_RATE,
                                "patch": s["patch"]})
            self.add_log(f"A{ag.id + 1}: miner online at ({s['x']},{s['y']})", "ev-build")
        self.structure_version += 1

    def sidestep(self, ag):
        order = list(C.DIRS)
        # Fisher–Yates with the seeded runtime stream — deterministic shuffle
        for i in range(len(order) - 1, 0, -1):
            j = int(self.sim_rnd() * (i + 1))
            order[i], order[j] = order[j], order[i]
        for dx, dy in order:
            sx, sy = ag.x + dx, ag.y + dy
            if not self.in_bounds(sx, sy):
                continue
            if self.base_cost(sx, sy) == float("inf"):
                continue
            if self.agent_at(sx, sy, ag) is not None:
                continue
            ag.x, ag.y = sx, sy
            return True
        return False

    def abort_task(self, ag, reason=None):
        self.release_reservations(ag)
        if ag.task and ag.task["type"] == "BUILD":
            self.stock["gears"] += ag.task["plan"]["cost"]
            ag.task["plan"]["patch"]["automated"] = False
            ag.plan = None
        if reason:
            self.add_log(f"A{ag.id + 1}: task aborted — {reason}", "ev-warn")
        ag.task = None
        ag.path = []
        ag.state = "PLAN"

    def agent_tick(self, ag):
        st = ag.state

        if st == "PLAN":
            self.release_reservations(ag)
            self.run_auction(ag)

        elif st == "MOVE":
            if not ag.path:
                self.arrive(ag)
                return
            nx, ny = ag.path[0]
            blocker = self.agent_at(nx, ny, ag)
            if blocker is not None:
                ag.wait_count += 1
                if blocker.state == "IDLE" and ag.wait_count >= 2:
                    self.sidestep(blocker)
                patience = 12 if (blocker.state == "MOVE" and ag.id < blocker.id) else 5
                if ag.wait_count >= patience:
                    self.sidestep(ag)
                    p = self.route(ag, ag.dest) if ag.dest else None
                    if p is not None:
                        ag.path = p
                        ag.wait_count = 0
                    else:
                        if ag.task:
                            ag.taboo_key = self.r_key(ag.task)
                            ag.taboo_until = self.tick + 200
                        self.abort_task(ag, "congestion")
                return
            ag.wait_count = 0
            ag.facing = ((nx > ag.x) - (nx < ag.x), (ny > ag.y) - (ny < ag.y))
            ag.x, ag.y = nx, ny
            ag.path.pop(0)
            if not ag.path:
                self.arrive(ag)

        elif st == "MINE":
            t = None
            if ag.task:
                tx, ty = ag.task["tile"]
                t = self.grid[ty][tx]
            if t is None or t["type"] != T.ORE or t["amount"] <= 0 or ag.cargo >= C.CARGO_CAP:
                ag.state = "PLAN"
                return
            ag.timer += 1
            if ag.timer >= C.MINE_TICKS:
                ag.timer = 0
                t["amount"] -= 1
                ag.cargo += 1
                if t["amount"] <= 0:
                    t["type"] = T.EMPTY

        elif st == "DELIVER":
            if ag.cargo > 0:
                ag.task["asm"]["buffer"] += ag.cargo
                self.stock["ore_delivered"] += ag.cargo
                self.add_log(f"A{ag.id + 1}: delivered {ag.cargo} ore by hand", "")
                ag.cargo = 0
                ag.tasks_done += 1
                self.stats["tasks_done"] += 1
            ag.state = "PLAN"

        elif st == "BUILD":
            ag.timer += 1
            if ag.timer >= C.BUILD_TICKS:
                ag.timer = 0
                self.place_structure(ag, ag.build_queue[ag.build_idx])
                ag.build_idx += 1
                self.step_build_queue(ag)

        elif st == "REPAIR":
            f = ag.task["fault"]
            ag.timer += 1
            if ag.timer >= C.REPAIR_TICKS:
                ag.timer = 0
                t = self.grid[f["y"]][f["x"]]
                t["type"] = T.BELT
                t["dir"] = f["dir"]
                if f in self.faults:
                    self.faults.remove(f)
                self.stats["faults_fixed"] += 1
                self.stats["mttr_sum"] += self.tick - f["created"]
                ag.tasks_done += 1
                self.stats["tasks_done"] += 1
                self.structure_version += 1
                self.add_log(f"A{ag.id + 1}: belt repaired at ({f['x']},{f['y']}) — "
                             f"down {(self.tick - f['created']) / 30:.1f}s", "ev-fix")
                self.release_reservations(ag)
                ag.task = None
                ag.state = "PLAN"

        elif st == "IDLE":
            ag.timer -= 1
            if ag.timer <= 0:
                ag.state = "PLAN"

    def arrive(self, ag):
        t = ag.task
        if t is None:
            if ag.build_queue and ag.build_idx < len(ag.build_queue):
                return
            ag.state = "PLAN"
            return
        k = t["type"]
        if k == "MINE":
            ag.state, ag.timer = "MINE", 0
        elif k == "DELIVER":
            ag.state = "DELIVER"
        elif k == "BUILD":
            ag.state, ag.timer = "BUILD", 0
        elif k == "REPAIR":
            ag.state, ag.timer = "REPAIR", 0
        elif k == "SUPERVISE":
            ag.state, ag.timer = "IDLE", 15
        else:
            ag.state = "PLAN"

    # ---------------- fault system ----------------

    def inject_fault(self, x=None, y=None):
        """Sever a belt so the next integrity scan raises a repairable fault.

        Two paths that must never bleed into each other:

        - Random (x/y omitted): chaos mode and the untargeted button. Drawn
          from the seeded runtime RNG; 300k+ ticks of regression prove it
          never picks an unreachable tile, so it needs no pre-check. Returns
          True/False, exactly as before — the deterministic fingerprint and
          test_fault_recovery both depend on this contract being unchanged.
        - Targeted (x, y given): a user click carries no such guarantee, so
          the tile is validated (must be an un-faulted belt) and
          reachability-checked (some agent must be able to path onto it)
          before severing. Consumes no RNG, so it can never perturb the
          random path. Returns a {"ok", "reason"} dict for the caller to
          surface a rejection.
        """
        if x is not None and y is not None:
            return self._inject_fault_at(int(x), int(y))

        belt_tiles = [b for L in self.lines for b in L["belts"]
                      if self.grid[b["y"]][b["x"]]["type"] == T.BELT]
        if not belt_tiles:
            self.add_log("No belts to sabotage yet.", "ev-warn")
            return False
        b = belt_tiles[int(self.sim_rnd() * len(belt_tiles))]
        self.grid[b["y"]][b["x"]]["type"] = T.EMPTY
        self.structure_version += 1
        self.add_log(f"FAULT INJECTED: belt severed at ({b['x']},{b['y']})", "ev-warn")
        log.warning("fault injected",
                    extra={"fault_x": b["x"], "fault_y": b["y"], "tick": self.tick})
        return True

    def _inject_fault_at(self, x, y):
        """Validate and sever a user-targeted belt tile. Returns
        {"ok": bool, "reason": str|None}; on any rejection the grid is left
        untouched so no fault is ever half-created."""
        if not self.in_bounds(x, y):
            return {"ok": False, "reason": "off the grid"}
        tile = self.grid[y][x]
        if tile["type"] != T.BELT:
            return {"ok": False, "reason": "not a belt tile"}
        if any(f["x"] == x and f["y"] == y for f in self.faults):
            return {"ok": False, "reason": "belt is already faulted"}
        if not self._agent_can_reach(x, y):
            return {"ok": False, "reason": "no agent can reach this location"}
        tile["type"] = T.EMPTY
        self.structure_version += 1
        self.add_log(f"FAULT INJECTED: belt severed at ({x},{y})", "ev-warn")
        log.warning("fault injected",
                    extra={"fault_x": x, "fault_y": y, "tick": self.tick,
                           "targeted": True})
        return {"ok": True, "reason": None}

    def _agent_can_reach(self, x, y):
        """True if any agent can path onto (x, y) — the exact tile a REPAIR
        task routes to. Reachability is cost-independent, so testing the belt
        at its current cost gives the same answer it will have once severed
        to empty ground."""
        for ag in self.agents:
            if pathfinding.astar(self, ag.x, ag.y,
                                 lambda cx, cy: cx == x and cy == y,
                                 (x, y), ag) is not None:
                return True
        return False

    def integrity_scan(self):
        for L in self.lines:
            for b in L["belts"]:
                if self.grid[b["y"]][b["x"]]["type"] != T.EMPTY:
                    continue
                if any(f["x"] == b["x"] and f["y"] == b["y"] for f in self.faults):
                    continue
                self.faults.append({"x": b["x"], "y": b["y"],
                                    "dir": b["dir"], "created": self.tick})
                self.add_log(f"Integrity scan: break detected at ({b['x']},{b['y']})",
                             "ev-warn")

    # ---------------- world tick ----------------

    def world_tick(self):
        if self.tick % 10 == 0:
            self.integrity_scan()
        if self.faults:
            self.stats["fault_ticks"] += 1
        if self.chaos_on and self.tick % 900 == 0 and self.tick > 0:
            self.inject_fault()

        for m in self.miners:
            m["cd"] -= 1
            if m["cd"] > 0:
                continue
            src = None
            for (tx, ty) in m["patch"]["tiles"]:
                t = self.grid[ty][tx]
                if t["type"] in (T.ORE, T.MINER) and t["amount"] > 0:
                    src = t
                    break
            if src is None:
                continue
            key = (m["out_x"], m["out_y"])
            if key not in self.occupied and \
               self.grid[m["out_y"]][m["out_x"]]["type"] == T.BELT:
                self.belt_items.append({"x": m["out_x"], "y": m["out_y"], "prog": 0.0})
                self.occupied.add(key)
                src["amount"] -= 1
                if src["amount"] <= 0 and src["type"] == T.ORE:
                    src["type"] = T.EMPTY
                m["cd"] = C.MINER_RATE

        for i in range(len(self.belt_items) - 1, -1, -1):
            it = self.belt_items[i]
            tile = self.grid[it["y"]][it["x"]]
            if tile["type"] != T.BELT:
                self.occupied.discard((it["x"], it["y"]))
                self.belt_items.pop(i)
                continue
            dx, dy = C.DIRS[tile["dir"]]
            nx, ny = it["x"] + dx, it["y"] + dy
            n_type = self.grid[ny][nx]["type"] if self.in_bounds(nx, ny) else T.ROCK
            it["prog"] = min(it["prog"] + 1 / 12, 1.0)
            if it["prog"] < 1.0:
                continue
            if n_type == T.ASM:
                asm = next(a for a in self.assemblers if a["x"] == nx and a["y"] == ny)
                asm["buffer"] += 1
                self.stock["ore_delivered"] += 1
                self.occupied.discard((it["x"], it["y"]))
                self.belt_items.pop(i)
            elif n_type == T.BELT and (nx, ny) not in self.occupied:
                self.occupied.discard((it["x"], it["y"]))
                it["x"], it["y"], it["prog"] = nx, ny, 0.0
                self.occupied.add((nx, ny))

        for a in self.assemblers:
            if a["buffer"] >= C.CRAFT_IN:
                a["progress"] += 1
                if a["progress"] >= C.CRAFT_TICKS:
                    a["progress"] = 0
                    a["buffer"] -= C.CRAFT_IN
                    a["crafted"] += 1
                    self.stock["gears"] += 1
                    self.gear_history.append(self.tick)
            else:
                a["progress"] = max(0, a["progress"] - 1)
        while self.gear_history and self.gear_history[0] < self.tick - 1800:
            self.gear_history.pop(0)

    # ---------------- public API ----------------

    def step(self):
        self.tick += 1
        self.world_tick()
        for ag in self.agents:
            self.agent_tick(ag)

    def run(self, n):
        for _ in range(n):
            self.step()

    def total_crafted(self):
        return sum(a["crafted"] for a in self.assemblers)

    def mttr_seconds(self):
        if not self.stats["faults_fixed"]:
            return None
        return self.stats["mttr_sum"] / self.stats["faults_fixed"] / 30

    def uptime_pct(self):
        return 100 * (1 - self.stats["fault_ticks"] / self.tick) if self.tick else 100.0

    def automated_count(self):
        return sum(1 for p in self.patches if p["automated"] or p["no_auto"])

    # ---------------- snapshot serialization ----------------

    def to_dict(self):
        """Serialize a *consistent* snapshot. In-flight construction is
        rolled back (refund + un-reserve) like an uncommitted transaction;
        agents resume from PLAN on load."""
        gears = self.stock["gears"]
        patch_auto = [p["automated"] for p in self.patches]
        for ag in self.agents:
            if ag.task and ag.task["type"] == "BUILD" and ag.plan:
                gears += ag.plan["cost"]
                idx = self.patches.index(ag.plan["patch"])
                patch_auto[idx] = False
        return {
            "seed": self.seed,
            "sim_seed": self.sim_seed,
            "tick": self.tick,
            "chaos_on": self.chaos_on,
            "grid": [[[t["type"], t["dir"], t["amount"]] for t in row]
                     for row in self.grid],
            "patches": [{"tiles": p["tiles"], "automated": patch_auto[i],
                         "no_auto": p["no_auto"]}
                        for i, p in enumerate(self.patches)],
            "assemblers": self.assemblers,
            "miners": [{"x": m["x"], "y": m["y"], "out_x": m["out_x"],
                        "out_y": m["out_y"], "cd": m["cd"],
                        "patch_idx": self.patches.index(m["patch"])}
                       for m in self.miners],
            "belt_items": self.belt_items,
            "lines": self.lines,
            "faults": self.faults,
            "stock": {"gears": gears, "ore_delivered": self.stock["ore_delivered"]},
            "stats": self.stats,
            "log": self.log[-30:],
            "agents": [{"id": a.id, "x": a.x, "y": a.y, "cargo": a.cargo,
                        "tasks_done": a.tasks_done} for a in self.agents],
        }

    @classmethod
    def from_dict(cls, d):
        sim = cls(d["seed"], _skip_world=True)
        sim.sim_seed = d["sim_seed"]
        sim.tick = d["tick"]
        sim.chaos_on = d["chaos_on"]
        sim.grid = [[{"type": t, "dir": dr, "amount": a} for (t, dr, a) in row]
                    for row in d["grid"]]
        sim.patches = [{"tiles": [tuple(t) for t in p["tiles"]],
                        "automated": p["automated"], "no_auto": p["no_auto"]}
                       for p in d["patches"]]
        sim.assemblers = [dict(a) for a in d["assemblers"]]
        sim.miners = [{"x": m["x"], "y": m["y"], "out_x": m["out_x"],
                       "out_y": m["out_y"], "cd": m["cd"],
                       "patch": sim.patches[m["patch_idx"]]}
                      for m in d["miners"]]
        sim.belt_items = [dict(b) for b in d["belt_items"]]
        sim.occupied = {(b["x"], b["y"]) for b in sim.belt_items}
        sim.lines = [{"belts": [dict(b) for b in L["belts"]]} for L in d["lines"]]
        sim.faults = [dict(f) for f in d["faults"]]
        sim.stock = dict(d["stock"])
        sim.stats = dict(d["stats"])
        sim.log = list(d["log"])
        sim.log_seq = len(sim.log)
        starts = d["agents"]
        sim.agents = []
        for a in starts:
            ag = Agent(a["id"], a["x"], a["y"])
            ag.cargo = a["cargo"]
            ag.tasks_done = a["tasks_done"]
            sim.agents.append(ag)
        sim.structure_version = 1
        sim.add_log(f"Snapshot restored at tick {sim.tick}.", "ev-build")
        return sim
