"""Wide regression on the Python core: 16 seeded worlds, bootstrapped then
sabotaged. Run from backend/: python ../scripts/regress.py"""

import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.sim.core import Simulation

SEEDS = [1337, 7, 424242, 90210, 555, 31415, 271828, 8675309,
         1, 2024, 99999, 123456, 777, 314159, 60606, 42]

t0 = time.time()
passed = 0
print("seed     | auto | miners | fixed | MTTR  | gears | result")
print("---------|------|--------|-------|-------|-------|-------")
for seed in SEEDS:
    sim = Simulation(seed)
    sim.run(12000)
    for _ in range(5):
        sim.inject_fault()
        sim.run(1200)
    sim.run(6000)
    ok = (not sim.faults and sim.total_crafted() > 300
          and sim.automated_count() == 5)
    passed += ok
    mttr = sim.mttr_seconds()
    print(f"{seed:<8} | {sim.automated_count()}/5  | {len(sim.miners):<6} | "
          f"{sim.stats['faults_fixed']:<5} | "
          f"{(f'{mttr:.1f}s' if mttr else '—'):<5} | "
          f"{sim.total_crafted():<5} | {'PASS' if ok else 'FAIL'}")

print(f"\n{passed}/{len(SEEDS)} seeds pass in {time.time()-t0:.1f}s")
sys.exit(0 if passed == len(SEEDS) else 1)
