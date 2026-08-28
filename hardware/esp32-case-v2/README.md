# ESP-32 smoker relay snap case v2

**v2 is the snap-fit fix Brandon printed and approved.** Folded into this repo from Cade’s package (Mac `~/github/esp32-smoker-case-v2` print files plus the fuller STEP/drawings pack).


Two-piece snap-fit clamshell for Brandon’s **NodeMCU ESP-32S V1.1** smoker relay (USB-C, dual 19-pin headers). **No screws.** USB-C cable stays plugged in. Pins fully enclosed. No BOOT/EN holes.

## What changed (v1 → v2 fit fix)

Printed v1 cover snap tabs could **not** slide past the board and lock. Root cause: v1 snap fingers sat **inside** the PCB outline (PCB→finger = **-0.90 mm**, i.e. overlap). Diamond barbs / stems hit the PCB edge, header plastic, USB jack, or BOOT/EN before they could flex into the base windows.

| Fix | v1 | v2 |
|---|---:|---:|
| PCB → snap finger inner | -0.90 mm (overlap) | **1.30 mm** clear |
| Snap hinge | at parting rim | **+2.0 mm** into lid (above header tops) |
| XY PCB pocket clear | 0.40 | **0.80** (+0.40) |
| Inner W / Outer W | 26.2 / 30.2 | **30.4 / 34.4** |
| Hook L / barb | 6.2 / 0.90 | **7.0 / 0.95** |
| Base windows | 8.8×3.6 | **9.6×4.2** |
| Base changed? | — | **YES** (wider cavity, corner stops, bigger windows) |

Kept: dual 19-pin enclosed, USB-C overmold opening only, LED membrane, 4 snaps, no screws, K1C/PETG 45°/45° barbs, **same ASSUMED board dims + caliper flags as v1**.

Regenerate:

```bash
python3 generate_package.py   # from this directory
```

## What prints

| STL | Print orientation | BBox mm | Notes |
|---|---|---|---|
| `stl/bottom.stl` | floor on bed, cavity up | 54.1 × 34.4 × 13.4 | 4 wider snap windows; corner stops |
| `stl/lid.stl` | **outer face on bed**, hooks up | 54.1 × 34.4 × 13.8 | hooks in flex channel, raised hinge |

Also: `3mf/bottom.3mf`, `3mf/lid.3mf` (if trimesh export succeeded).

K1C XY limit 215 mm. PLA or PETG, **0.20 mm layer, 0.4 mm nozzle**, 4–5 perimeters, 20% infill, **no supports**. PETG preferred for snaps.

## Assembly (tabs clear board, then click)

1. Print both STLs in the orientations above.
2. Drop the board into the bottom, **pins down** on the four posts. Corner stops locate XY in the wider pocket. USB toward the U-slot; cable can stay plugged in.
3. Lower the lid: the four snap fingers travel in the **side flex channels**, clearing the PCB edge / headers / USB / BOOT / EN.
4. Press until **four clicks** — barbs enter the base windows below board level.
5. To open: pinch the long-side windows and lift the lid.

Debug / flash: unplug, take lid off, USB to Mac. **No BOOT/EN holes.**

## Assumed board vs photo-scale (unchanged from v1)

| Dim | Design mm | Status |
|---|---:|---|
| PCB L × W × T | 48.50 × 25.40 × 1.60 | **ASSUMED** |
| Pin pitch / count / span | 2.54 / 19 / 45.72 | KNOWN |
| Pin row centers | 22.86 (0.9″) | **ASSUMED** |
| Header plastic H | 2.54 | **ASSUMED** |
| Pin stick-out | 6.00 | **ASSUMED FLAG** |
| Shield-can H | 3.50 | **ASSUMED FLAG** |
| USB-C overmold | 12.40 × 7.50 | H **ASSUMED FLAG** |

Internal PCB pocket: outline +0.80 mm. Snap flex channel adds long-side width so finger inner face is **1.30 mm** outside the PCB edge. Pins hang in an 8.0 mm well and do **not** poke out.

## USB-C / LED / snaps

- **USB-C opening only**: 13.6 × 8.8 mm rounded (R1.8), 0.8 mm lead-in, split across parting line.
- **LED**: 5 mm Ø × 0.6 mm membrane over EN-side red LED (no through-hole).
- **Snaps**: 4 diamond-barb hooks, 8 mm wide, 0.95 mm barb, 45°/45°, hinge 2.0 mm above rim. Through-windows for fingernail release.

## Clearance report (for parent → Rhea)

- **PCB edge → snap finger inner face**: **1.30 mm** (v1 was -0.90 mm overlap)
- **Snap hinge vs header top**: hinge at Z=15.40, header top Z=14.14 → **+1.26 mm** above headers
- **Barb catch vs board top**: catch Z=8.30, board top Z=11.60 → barb locks **3.30 mm below** board top (after stem has cleared laterally)
- **Base changed**: **YES**

## Caliper flags (Rhea → Brandon, same 3 shots)

1. **PCB length × width** (clone spread 48.3–51 mm).
2. **Pin stick-out** from PCB bottom to pin tip (design 6.0 mm).
3. **USB-C overmold width × height** with the plug fully seated (design 12.4 × 7.5 mm).

## Files

- `stl/bottom.stl` `stl/lid.stl` — print orientation (bed at Z=0)
- `3mf/bottom.3mf` `3mf/lid.3mf` — same meshes when export works
- `step/bottom.step` `step/lid.step` — assembled orientation
- `drawings/esp32-smoker-case-v2.pdf` + PNG sheets (snap section + delta)
- `dims.json` `bom.csv` `print_list.csv`
- `generate_package.py` — this package

v1 was not copied into this repo.
