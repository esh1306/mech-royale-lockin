# Mech Royale bot — `lockin`

Heuristic behavior AI for the Builders Buildathon **Mech Royale** track
(`mechfight/1` protocol). Pilots one mech in a 16-way 3D battle royale.

## Design
A priority-ordered behavior state machine:

1. **Zone safety** — always stay inside the (next) shrinking circle, building-aware.
2. **Dodge** — strafe/break away from incoming mortar shells and charging lasers.
3. **Retreat & heal** — when low HP and pressured, break line of sight to trigger regen.
4. **Engage** — lead-aimed gun fire at favorable, visible targets; face threats (rear armor).
5. **Farm** — supply drops, cubes, crates, and laser loot to snowball HP/damage.

A static `WorldModel` parses `/arena` (`v2/world.json` prisms + `v2/terrain.i16`) once
for line-of-sight-of-fire and ground height. All decisions run in well under the 50 ms budget
(local p99 ≈ 0.2 ms).

## Build & run
```sh
docker build -t lockin .
python3 check/check.py -- python3 -u bot.py          # protocol conformance
```

## Libraries
None beyond the Python standard library. `mechfight.py` is the official kit wrapper
(stdin/stdout loop + typed messages); the bot logic in `bot.py` is our own work.

## License
MIT — see `LICENSE`.
