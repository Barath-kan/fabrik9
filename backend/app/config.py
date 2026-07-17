"""Simulation constants — mirrors the frontend's config.js exactly."""

COLS, ROWS = 42, 28

class T:
    EMPTY, ORE, ROCK, BELT, MINER, ASM = 0, 1, 2, 3, 4, 5

DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1)]  # E W S N

N_AGENTS     = 3
CARGO_CAP    = 10
ORE_PER_TILE = 400
CRAFT_TICKS  = 40
CRAFT_IN     = 2
MINE_TICKS   = 14
MINER_RATE   = 55
BUILD_TICKS  = 16
REPAIR_TICKS = 24

COST = {"MINER": 8, "BELT": 1, "ASM": 14}
HQ = (20, 13)

TICKS_PER_SEC = 30
