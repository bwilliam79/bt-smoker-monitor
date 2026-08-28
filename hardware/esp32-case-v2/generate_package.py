#!/usr/bin/env python3
"""ESP-32 smoker-relay snap case v2 — fit fix for snap tabs vs PCB.

v1 problem: snap fingers sat inside the PCB outline (~0.9 mm overlap).
Lid barbs/walls hit PCB edge, header plastic, USB, BOOT/EN before flexing
into the base windows.

v2 fix (simplest that works):
  1. Push snaps outward into a dedicated flex channel (≥1.3 mm PCB→finger).
  2. Raise snap hinge into the lid so flex starts above header tops.
  3. Widen/deepen base windows for the extra travel.
  4. Bump PCB pocket XY clear 0.40 → 0.80 mm; corner stops locate the board.
  5. No BOOT/EN holes; USB-C only; LED membrane kept; 4 snaps; no screws.

Regenerate:
    python3 /workspace/esp32-smoker-case-v2/generate_package.py
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import cadquery as cq
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import (
    FancyBboxPatch,
    Rectangle,
    Circle,
    Polygon,
)
import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parent
OUT_STL = ROOT / "stl"
OUT_STEP = ROOT / "step"
OUT_DRAW = ROOT / "drawings"
OUT_3MF = ROOT / "3mf"
for p in (OUT_STL, OUT_STEP, OUT_DRAW, OUT_3MF):
    p.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Geometry (mm). ASSUMED board envelope unchanged from v1 (same caliper flags).
# ---------------------------------------------------------------------------
WALL = 2.00
FILLET_XY = 2.00

PCB_L = 48.50  # ASSUMED (same as v1)
PCB_W = 25.40  # ASSUMED / published
PCB_T = 1.60  # ASSUMED FR4
XY_CLEAR = 0.80  # v1 was 0.40; +0.40 so board drops easily but still locates
PIN_PITCH = 2.54  # KNOWN
PIN_N = 19  # KNOWN
PIN_SPAN = (PIN_N - 1) * PIN_PITCH  # 45.72
PIN_ROW_C = 22.86  # ASSUMED 0.9" NodeMCU-32S
PIN_STICK = 6.00  # ASSUMED FLAG
PIN_EXTRA = 2.00
HEADER_H = 2.54  # ASSUMED
SHIELD_H = 3.50  # ASSUMED FLAG
TOP_CLEAR = 1.20

PIN_WELL = PIN_STICK + PIN_EXTRA  # 8.00
COMP_WELL = max(HEADER_H, SHIELD_H) + TOP_CLEAR  # 4.70

# Snap channel: PCB edge → finger inner face ≥ 1.2 mm (use 1.30)
SNAP_CLEAR = 1.30
HOOK_T = 1.20  # finger thickness (radial)
HOOK_W = 8.00
HOOK_L = 7.00  # tip distance below rim (was 6.20)
HOOK_BARB = 0.95  # was 0.90; slight bump with wider window
HOOK_ROOT_ABOVE_RIM = 2.00  # hinge lives in lid, above header tops
SNAP_N = 4
SNAP_X = 12.00
WIN_W = 9.60  # was 8.80
WIN_H = 4.20  # was 3.60
LIP_H = 1.40
LIP_T = 1.20
LIP_GAP = 0.30

# Inner cavity is wide enough for the snap channel on the long sides.
# Finger inner face at PCB_W/2 + SNAP_CLEAR; attach at inner wall = that + HOOK_T.
FINGER_INNER = PCB_W / 2.0 + SNAP_CLEAR  # 14.00
INNER_W = 2.0 * (FINGER_INNER + HOOK_T)  # 30.40
INNER_L = PCB_L + 2.0 * XY_CLEAR  # 50.10
OUTER_L = INNER_L + 2.0 * WALL  # 54.10
OUTER_W = INNER_W + 2.0 * WALL  # 34.40

PCB_POCKET_L = PCB_L + 2.0 * XY_CLEAR  # 50.10
PCB_POCKET_W = PCB_W + 2.0 * XY_CLEAR  # 27.00

# Parting / Z stack (same vertical stack as v1)
RIM_ABOVE_PCB = 1.80
BOTTOM_INNER_H = PIN_WELL + PCB_T + RIM_ABOVE_PCB  # 11.40
BOTTOM_OUTER_H = WALL + BOTTOM_INNER_H  # 13.40

USB_OVER_W = 12.40
USB_OVER_H = 7.50
USB_METAL_H = 3.20
USB_OPEN_W = 13.60
USB_OPEN_H = 8.80
USB_OPEN_R = 1.80
USB_CHAMFER = 0.80
USB_ZC_ABOVE_PCB_TOP = USB_METAL_H / 2.0  # 1.60
OVERMOLD_TOP_ABOVE_PCB = USB_ZC_ABOVE_PCB_TOP + USB_OPEN_H / 2.0  # 6.00
LID_INNER_H = max(
    COMP_WELL - RIM_ABOVE_PCB,
    OVERMOLD_TOP_ABOVE_PCB - RIM_ABOVE_PCB + 0.6,
)  # 4.80
LID_OUTER_H = WALL + LID_INNER_H  # 6.80

LED_YES = True
LED_D = 5.00
LED_MEMBRANE = 0.60
LED_FROM_USB = 11.00
LED_FROM_EN_EDGE = 7.50

POST_X_USB = 9.00
POST_X_ANT = 3.50
POST_Y = 5.40
POST_W = 3.20
POST_D = 6.00

# Corner stops (locate PCB in the wider cavity; leave mid-side channel for snaps)
STOP_L = 4.00  # along X
STOP_W = 2.20  # along Y (from pocket edge inward toward wall channel)
STOP_H_EXTRA = 0.30  # above PCB top

LAYER = 0.20
NOZZLE = 0.40
K1C_XY = 215.0

Z_FLOOR = WALL
Z_PCB_BOT = WALL + PIN_WELL
Z_PCB_TOP = Z_PCB_BOT + PCB_T
Z_RIM = BOTTOM_OUTER_H
Z_USB_C = Z_PCB_TOP + USB_ZC_ABOVE_PCB_TOP
Z_USB_BOT = Z_USB_C - USB_OPEN_H / 2.0
Z_USB_TOP = Z_USB_C + USB_OPEN_H / 2.0
Z_ASSY_TOP = Z_RIM + LID_OUTER_H
Z_HEADER_TOP = Z_PCB_TOP + HEADER_H
Z_HOOK_ROOT = Z_RIM + HOOK_ROOT_ABOVE_RIM
Z_BARB_TIP = Z_RIM - HOOK_L
Z_BARB_CATCH = Z_RIM - (HOOK_L - 2.0 * HOOK_BARB)

LED_X = -PCB_L / 2.0 + LED_FROM_USB
LED_Y = -(PCB_W / 2.0 - LED_FROM_EN_EDGE)

# v1 deltas (for drawings / README)
V1_XY_CLEAR = 0.40
V1_INNER_W = 26.20
V1_OUTER_W = 30.20
V1_HOOK_L = 6.20
V1_HOOK_BARB = 0.90
V1_HOOK_T = 1.30
V1_WIN_W = 8.80
V1_WIN_H = 3.60
V1_PCB_TO_FINGER = (V1_INNER_W / 2.0 - V1_HOOK_T) - (PCB_W / 2.0)  # -0.90

REV = "v2"
TITLE = "ESP-32 smoker relay snap case"


def _fillet_safe(wp: cq.Workplane, selector: str, r: float) -> cq.Workplane:
    try:
        return wp.edges(selector).fillet(r)
    except Exception:
        return wp


def _chamfer_safe(wp: cq.Workplane, selector: str, d: float) -> cq.Workplane:
    try:
        return wp.edges(selector).chamfer(d)
    except Exception:
        return wp


def usb_cutter() -> cq.Workplane:
    length = WALL * 4.0 + 4.0
    x_center = -OUTER_L / 2.0
    body = (
        cq.Workplane("YZ")
        .workplane(offset=x_center)
        .center(0.0, Z_USB_C)
        .rect(USB_OPEN_W, USB_OPEN_H)
        .extrude(length / 2.0, both=True)
    )
    body = _fillet_safe(body, "|X", USB_OPEN_R)
    lead_w = USB_OPEN_W + 2 * USB_CHAMFER
    lead_h = USB_OPEN_H + 2 * USB_CHAMFER
    lead = (
        cq.Workplane("YZ")
        .workplane(offset=-OUTER_L / 2.0 - 0.05)
        .center(0.0, Z_USB_C)
        .rect(lead_w, lead_h)
        .workplane(offset=USB_CHAMFER + 0.2)
        .center(0.0, Z_USB_C)
        .rect(USB_OPEN_W, USB_OPEN_H)
        .loft(combine=True)
    )
    try:
        lead = _fillet_safe(lead, "|X", USB_OPEN_R)
    except Exception:
        pass
    try:
        return body.union(lead)
    except Exception:
        return body


def pcb_posts() -> cq.Workplane:
    xs = [-PCB_L / 2.0 + POST_X_USB, PCB_L / 2.0 - POST_X_ANT]
    ys = [POST_Y, -POST_Y]
    solid = None
    for x in xs:
        for y in ys:
            post = (
                cq.Workplane("XY")
                .transformed(offset=(x, y, Z_FLOOR))
                .rect(POST_W, POST_D)
                .extrude(PIN_WELL)
            )
            post = _chamfer_safe(post, ">Z", 0.4)
            solid = post if solid is None else solid.union(post)
    return solid


def pcb_corner_stops() -> cq.Workplane:
    """Locate the PCB in the wider cavity; mid-side channels stay open for snaps."""
    solid = None
    h = PIN_WELL + PCB_T + STOP_H_EXTRA
    # Outer face of stop at pocket outline; stop extends inward toward PCB center
    for x_sign in (1.0, -1.0):
        for y_sign in (1.0, -1.0):
            # Block sitting just outside the PCB, inside the pocket outline
            x = x_sign * (PCB_L / 2.0 + XY_CLEAR / 2.0)
            y = y_sign * (PCB_W / 2.0 + XY_CLEAR / 2.0)
            stop = (
                cq.Workplane("XY")
                .transformed(offset=(x, y, Z_FLOOR))
                .rect(XY_CLEAR + 0.05, XY_CLEAR + 0.05)
                .extrude(h)
            )
            # Longer rail stub along X at the long-edge pocket (not at snap X)
            # End zones only: from antenna/USB corners inward ~STOP_L
            x_rail = x_sign * (PCB_L / 2.0 - STOP_L / 2.0 + 0.5)
            y_rail = y_sign * (PCB_W / 2.0 + XY_CLEAR / 2.0)
            rail = (
                cq.Workplane("XY")
                .transformed(offset=(x_rail, y_rail, Z_FLOOR))
                .rect(STOP_L, XY_CLEAR + 0.05)
                .extrude(h)
            )
            piece = stop.union(rail)
            solid = piece if solid is None else solid.union(piece)
    return solid


def snap_window_cutters() -> cq.Workplane:
    z_tip = Z_BARB_TIP
    z_catch = Z_BARB_CATCH
    z_c = 0.5 * (z_tip + z_catch) + 0.20
    solid = None
    for x in (SNAP_X, -SNAP_X):
        for sign in (1.0, -1.0):
            win = (
                cq.Workplane("XZ")
                .workplane(offset=sign * OUTER_W / 2.0)
                .center(x, z_c)
                .rect(WIN_W, WIN_H)
                .extrude(-sign * (WALL + 0.8))
            )
            solid = win if solid is None else solid.union(win)
    return solid


def make_hooks() -> cq.Workplane:
    """Four diamond-barb hooks in the flex channel (outside PCB outline).

    Profile in local YZ with y=0 at the inner wall face, −y inward (toward PCB),
    +y outward into the wall window. Root starts HOOK_ROOT_ABOVE_RIM above the
    parting line so the hinge is above header tops.
    """
    t = HOOK_T
    L = HOOK_L
    b = HOOK_BARB
    root = HOOK_ROOT_ABOVE_RIM

    def one_side(y_sign: float) -> cq.Workplane:
        # z=0 at rim; +z up into lid; −z down into base
        pts = [
            (0.0, root),
            (-t, root),
            (-t, -L),
            (0.0, -L),
            (b, -L + b),
            (0.0, -L + 2 * b),
            (0.0, root),
        ]
        solid = None
        for x in (SNAP_X, -SNAP_X):
            h = (
                cq.Workplane("YZ")
                .polyline(pts)
                .close()
                .extrude(HOOK_W / 2.0, both=True)
                .translate((x, INNER_W / 2.0, Z_RIM))
            )
            solid = h if solid is None else solid.union(h)
        if y_sign < 0:
            solid = solid.mirror("XZ", union=False)
        return solid

    return one_side(+1.0).union(one_side(-1.0))


def make_lid_lip() -> cq.Workplane:
    rail_l = INNER_L - 6.0
    rail_w = LIP_T
    y = INNER_W / 2.0 - LIP_GAP - rail_w / 2.0
    solid = None
    for sign in (1.0, -1.0):
        rail = (
            cq.Workplane("XY")
            .transformed(offset=(1.0, sign * y, Z_RIM - LIP_H))
            .rect(rail_l, rail_w)
            .extrude(LIP_H + 0.2)
        )
        solid = rail if solid is None else solid.union(rail)
    return solid


def build_bottom() -> cq.Workplane:
    outer = (
        cq.Workplane("XY")
        .rect(OUTER_L, OUTER_W)
        .extrude(BOTTOM_OUTER_H)
    )
    outer = _fillet_safe(outer, "|Z", FILLET_XY)
    cavity = (
        cq.Workplane("XY")
        .transformed(offset=(0, 0, Z_FLOOR))
        .rect(INNER_L, INNER_W)
        .extrude(BOTTOM_INNER_H + 1.0)
    )
    cavity = _fillet_safe(cavity, "|Z", max(0.6, FILLET_XY - WALL))
    body = outer.cut(cavity)
    body = body.union(pcb_posts())
    body = body.union(pcb_corner_stops())
    body = body.cut(usb_cutter())
    body = body.cut(snap_window_cutters())
    body = _chamfer_safe(body, ">Z", 0.3)
    return body


def build_lid() -> cq.Workplane:
    block = (
        cq.Workplane("XY")
        .rect(OUTER_L, OUTER_W)
        .extrude(LID_OUTER_H)
    )
    block = _fillet_safe(block, "|Z", FILLET_XY)
    lid = block.translate((0, 0, Z_RIM))
    cav = (
        cq.Workplane("XY")
        .transformed(offset=(0, 0, Z_RIM - 0.2))
        .rect(INNER_L, INNER_W)
        .extrude(LID_INNER_H + 0.2)
    )
    cav = _fillet_safe(cav, "|Z", max(0.6, FILLET_XY - WALL))
    lid = lid.cut(cav)
    lid = lid.union(make_lid_lip())
    lid = lid.union(make_hooks())
    lid = lid.cut(usb_cutter())
    if LED_YES:
        bore_d = LED_D
        bore_depth = WALL - LED_MEMBRANE
        z_inner_roof = Z_RIM + LID_INNER_H
        bore = (
            cq.Workplane("XY")
            .transformed(offset=(LED_X, LED_Y, z_inner_roof - 0.05))
            .circle(bore_d / 2.0)
            .extrude(bore_depth + 0.05)
        )
        lid = lid.cut(bore)
    lid = _chamfer_safe(lid, ">Z", 0.3)
    return lid


def export_stl(wp: cq.Workplane, path: Path, print_rotate: str | None = None) -> dict:
    solid = wp.val()
    if print_rotate == "lid":
        solid = solid.rotate((0, 0, 0), (1, 0, 0), 180)
    cq.exporters.export(solid, str(path))
    mesh = trimesh.load(str(path))
    mesh.apply_translation((0, 0, -mesh.bounds[0][2]))
    mesh.export(str(path))
    # 3MF sibling
    mpath = OUT_3MF / path.name.replace(".stl", ".3mf")
    try:
        mesh.export(str(mpath))
    except Exception as e:
        print(f"  3mf export skipped for {path.name}: {e}")
        mpath = None
    bb = mesh.bounds
    size = (bb[1] - bb[0]).tolist()
    return {
        "file": path.name,
        "bbox_mm": [round(v, 2) for v in size],
        "volume_mm3": round(float(mesh.volume), 1),
        "faces": int(len(mesh.faces)),
        "watertight": bool(mesh.is_watertight),
        "k1c_ok": all(s <= K1C_XY for s in size[:2]),
        "3mf": mpath.name if mpath else None,
    }


def dims_dict(bottom_info: dict, lid_info: dict) -> dict:
    pcb_to_finger = FINGER_INNER - PCB_W / 2.0
    return {
        "rev": REV,
        "title": TITLE,
        "assumed": True,
        "fit_fix": "v1 snaps overlapped PCB outline; v2 pushes snaps into flex channel",
        "wall_mm": WALL,
        "pcb_l_mm": PCB_L,
        "pcb_w_mm": PCB_W,
        "pcb_t_mm": PCB_T,
        "pcb_l_source": "ASSUMED typical NodeMCU-32S (published 48.26); +0.24 conservative",
        "pcb_w_source": "ASSUMED / published 25.4",
        "xy_clear_mm": XY_CLEAR,
        "xy_clear_v1_mm": V1_XY_CLEAR,
        "snap_clear_pcb_to_finger_mm": round(pcb_to_finger, 3),
        "snap_clear_v1_pcb_to_finger_mm": round(V1_PCB_TO_FINGER, 3),
        "pin_pitch_mm": PIN_PITCH,
        "pin_n": PIN_N,
        "pin_span_mm": PIN_SPAN,
        "pin_row_centers_mm": PIN_ROW_C,
        "pin_stick_mm": PIN_STICK,
        "pin_well_mm": PIN_WELL,
        "header_h_mm": HEADER_H,
        "shield_h_mm": SHIELD_H,
        "inner_l_mm": INNER_L,
        "inner_w_mm": INNER_W,
        "outer_l_mm": OUTER_L,
        "outer_w_mm": OUTER_W,
        "pcb_pocket_l_mm": PCB_POCKET_L,
        "pcb_pocket_w_mm": PCB_POCKET_W,
        "bottom_outer_h_mm": BOTTOM_OUTER_H,
        "lid_outer_h_mm": LID_OUTER_H,
        "assembled_h_mm": Z_ASSY_TOP,
        "usb_open_w_mm": USB_OPEN_W,
        "usb_open_h_mm": USB_OPEN_H,
        "usb_overmold_w_mm": USB_OVER_W,
        "usb_overmold_h_mm": USB_OVER_H,
        "snap_count": SNAP_N,
        "hook_w_mm": HOOK_W,
        "hook_l_mm": HOOK_L,
        "hook_t_mm": HOOK_T,
        "hook_barb_mm": HOOK_BARB,
        "hook_root_above_rim_mm": HOOK_ROOT_ABOVE_RIM,
        "win_w_mm": WIN_W,
        "win_h_mm": WIN_H,
        "z_pcb_bot": Z_PCB_BOT,
        "z_pcb_top": Z_PCB_TOP,
        "z_header_top": Z_HEADER_TOP,
        "z_hook_root": Z_HOOK_ROOT,
        "z_barb_tip": Z_BARB_TIP,
        "z_barb_catch": Z_BARB_CATCH,
        "z_rim": Z_RIM,
        "z_usb_c": Z_USB_C,
        "barb_below_board_top_mm": round(Z_PCB_TOP - Z_BARB_CATCH, 3),
        "hinge_above_header_top_mm": round(Z_HOOK_ROOT - Z_HEADER_TOP, 3),
        "base_changed": True,
        "led_window": LED_YES,
        "led_d_mm": LED_D,
        "led_membrane_mm": LED_MEMBRANE,
        "led_x_mm": LED_X,
        "led_y_mm": LED_Y,
        "bottom_stl": bottom_info,
        "lid_stl": lid_info,
        "caliper_flags": [
            "PCB length and width (clone spread 48.3–51 mm x ~25.4; design 48.5 x 25.4 +0.8 clear)",
            "Pin stick-out from PCB bottom to pin tip (design 6.0 mm + 2.0 mm well extra)",
            "USB-C cable overmold width x height with plug seated (design 12.4 x 7.5 mm; opening 13.6 x 8.8)",
        ],
        "v1_delta": {
            "xy_clear_mm": [V1_XY_CLEAR, XY_CLEAR],
            "pcb_to_finger_mm": [round(V1_PCB_TO_FINGER, 3), round(pcb_to_finger, 3)],
            "inner_w_mm": [V1_INNER_W, INNER_W],
            "outer_w_mm": [V1_OUTER_W, OUTER_W],
            "hook_l_mm": [V1_HOOK_L, HOOK_L],
            "hook_barb_mm": [V1_HOOK_BARB, HOOK_BARB],
            "win_mm": [[V1_WIN_W, V1_WIN_H], [WIN_W, WIN_H]],
            "hook_root_above_rim_mm": [0.0, HOOK_ROOT_ABOVE_RIM],
        },
        "photo_scale": {
            "reference": "USB-C overmold width 12.4 mm (typical) and 2.54 mm pin pitch",
            "method": "same ASSUMED envelope as v1; fit fix is snap channel geometry only",
        },
    }


# ---------------------------------------------------------------------------
# Drawings
# ---------------------------------------------------------------------------
INK = "#1a1a1a"
DIM = "#1a1a1a"
ASSUMED_C = "#b10000"
OK_C = "#0b5a2a"
FIX_C = "#0a3d7a"
NOTE_C = "#333333"
FILL_BOT = "#d9d5cf"
FILL_LID = "#c5d4e8"
FILL_PCB = "#3d7a4a"
FILL_PIN = "#b8b8b8"
FILL_CH = "#fff3bf"


def _dim_h(ax, x0, x1, y, text, color=DIM, side=1, fs=8):
    ax.annotate(
        "", xy=(x1, y), xytext=(x0, y),
        arrowprops=dict(arrowstyle="<->", color=color, lw=0.8),
    )
    ax.text(
        0.5 * (x0 + x1), y + side * 1.1, text,
        ha="center", va="bottom" if side > 0 else "top",
        fontsize=fs, color=color, clip_on=False,
    )


def _dim_v(ax, y0, y1, x, text, color=DIM, side=1, fs=8):
    ax.annotate(
        "", xy=(x, y1), xytext=(x, y0),
        arrowprops=dict(arrowstyle="<->", color=color, lw=0.8),
    )
    ax.text(
        x + side * 1.1, 0.5 * (y0 + y1), text,
        ha="left" if side > 0 else "right", va="center",
        fontsize=fs, color=color, clip_on=False,
    )


def _banner(fig, text):
    fig.text(
        0.5, 0.012, text, ha="center", va="bottom",
        fontsize=7.5, color=ASSUMED_C, fontweight="bold",
    )


def draw_page1(fig):
    fig.clear()
    fig.suptitle(
        f"{TITLE}  {REV}   —   measured drawing (mm)   —   SNAP FIT FIX",
        fontsize=12, fontweight="bold", color=INK, y=0.98,
    )
    gs = fig.add_gridspec(
        2, 2, left=0.06, right=0.98, top=0.92, bottom=0.07,
        wspace=0.22, hspace=0.28,
    )

    # TOP
    ax = fig.add_subplot(gs[0, 0])
    ax.set_title("TOP  (lid on, USB −X)  —  snap channel outside PCB", fontsize=10, pad=6)
    ax.set_aspect("equal")
    hl, hw = OUTER_L / 2, OUTER_W / 2
    ax.add_patch(FancyBboxPatch(
        (-hl, -hw), OUTER_L, OUTER_W,
        boxstyle=f"round,pad=0,rounding_size={FILLET_XY}",
        facecolor=FILL_LID, edgecolor=INK, lw=1.2,
    ))
    # inner cavity / channel
    ax.add_patch(Rectangle(
        (-INNER_L / 2, -INNER_W / 2), INNER_L, INNER_W,
        facecolor=FILL_CH, edgecolor=FIX_C, lw=0.7, alpha=0.55, ls="--",
    ))
    ax.add_patch(Rectangle(
        (-PCB_L / 2, -PCB_W / 2), PCB_L, PCB_W,
        facecolor=FILL_PCB, edgecolor=INK, lw=0.6, alpha=0.4,
    ))
    # pocket outline
    ax.add_patch(Rectangle(
        (-PCB_POCKET_L / 2, -PCB_POCKET_W / 2), PCB_POCKET_L, PCB_POCKET_W,
        facecolor="none", edgecolor=OK_C, lw=0.8, ls=":",
    ))
    for s in (1, -1):
        y = s * PIN_ROW_C / 2
        ax.plot([-PIN_SPAN / 2, PIN_SPAN / 2], [y, y], color="#555", lw=1.4)
    ax.add_patch(Rectangle(
        (-hl - 0.2, -USB_OPEN_W / 2), WALL + 0.4, USB_OPEN_W,
        facecolor="#111", edgecolor=INK, lw=0.5, alpha=0.8,
    ))
    ax.text(-hl - 1.5, 0, "USB-C", ha="right", va="center", fontsize=7, color=INK, rotation=90)
    for x in (SNAP_X, -SNAP_X):
        for s in (1.0, -1.0):
            ax.plot(
                [x - HOOK_W / 2, x + HOOK_W / 2], [s * hw, s * hw],
                color=FIX_C, lw=3.5, solid_capstyle="round",
            )
            # finger body in channel: from FINGER_INNER out to INNER_W/2
            y_lo = s * FINGER_INNER if s > 0 else -INNER_W / 2
            ax.add_patch(Rectangle(
                (x - HOOK_W / 2, y_lo),
                HOOK_W, HOOK_T,
                facecolor=FIX_C, alpha=0.35, edgecolor=FIX_C, lw=0.5,
            ))
    ax.text(0, hw + 3.2, "4 snaps in flex channel (outside PCB)", ha="center", fontsize=7, color=FIX_C)
    ax.add_patch(Circle((LED_X, LED_Y), LED_D / 2, facecolor="#ff4a4a", edgecolor=INK, lw=0.5, alpha=0.85))
    ax.text(LED_X, LED_Y - 3.6, "LED 5 Ø × 0.6 membrane", ha="center", va="top", fontsize=6.5, color=ASSUMED_C)
    ax.text(0, -hw - 3.4, "NO BOOT / EN holes  —  USB-C opening only", ha="center", fontsize=7, color=NOTE_C)
    _dim_h(ax, -hl, hl, hw + 6.8, f"{OUTER_L:.2f} overall L  (v1 53.30)", fs=7.5)
    _dim_h(ax, -PCB_L / 2, PCB_L / 2, -hw - 7.0, f"{PCB_L:.2f} PCB L  ASSUMED", color=ASSUMED_C, side=-1, fs=7)
    _dim_v(ax, -hw, hw, hl + 5.0, f"{OUTER_W:.2f}\n(v1 30.20)", fs=7.5)
    _dim_v(ax, -PCB_W / 2, PCB_W / 2, -hl - 6.0, f"{PCB_W:.2f}\nASSUMED", color=ASSUMED_C, side=-1, fs=7)
    ax.set_xlim(-hl - 16, hl + 14)
    ax.set_ylim(-hw - 14, hw + 14)
    ax.axis("off")

    # FRONT USB
    ax = fig.add_subplot(gs[0, 1])
    ax.set_title("FRONT  USB end  (−X)   opening 13.6 × 8.8", fontsize=10, pad=6)
    ax.set_aspect("equal")
    ax.add_patch(FancyBboxPatch(
        (-hw, 0), OUTER_W, BOTTOM_OUTER_H,
        boxstyle="round,pad=0,rounding_size=1.0",
        facecolor=FILL_BOT, edgecolor=INK, lw=1.2,
    ))
    ax.add_patch(FancyBboxPatch(
        (-hw, Z_RIM), OUTER_W, LID_OUTER_H,
        boxstyle="round,pad=0,rounding_size=1.0",
        facecolor=FILL_LID, edgecolor=INK, lw=1.2,
    ))
    ax.add_patch(Rectangle(
        (-INNER_W / 2, Z_FLOOR), INNER_W, BOTTOM_INNER_H + LID_INNER_H,
        facecolor="none", edgecolor="#888", lw=0.5, ls="--",
    ))
    ax.add_patch(Rectangle(
        (-PCB_W / 2, Z_PCB_BOT), PCB_W, PCB_T,
        facecolor=FILL_PCB, edgecolor=INK, lw=0.5,
    ))
    for s in (1, -1):
        ax.add_patch(Rectangle(
            (s * PIN_ROW_C / 2 - 1.27, Z_PCB_TOP), 2.54, HEADER_H,
            facecolor="#222", edgecolor="none",
        ))
        ax.add_patch(Rectangle(
            (s * PIN_ROW_C / 2 - 0.3, Z_PCB_BOT - PIN_STICK), 0.6, PIN_STICK,
            facecolor=FILL_PIN, edgecolor="none",
        ))
    ax.add_patch(FancyBboxPatch(
        (-USB_OPEN_W / 2, Z_USB_BOT), USB_OPEN_W, USB_OPEN_H,
        boxstyle=f"round,pad=0,rounding_size={USB_OPEN_R}",
        facecolor="#111", edgecolor=INK, lw=0.8,
    ))
    ax.add_patch(FancyBboxPatch(
        (-USB_OVER_W / 2, Z_USB_C - USB_OVER_H / 2), USB_OVER_W, USB_OVER_H,
        boxstyle="round,pad=0,rounding_size=1.2",
        facecolor="#444", edgecolor="#eee", lw=0.4, alpha=0.55,
    ))
    ax.text(0, Z_USB_C, "overmold\n12.4 × 7.5 ASSUMED", ha="center", va="center", fontsize=6.5, color="white")
    ax.axhline(Z_RIM, color=FIX_C, lw=0.6, ls=":")
    _dim_v(ax, 0, Z_ASSY_TOP, hw + 5.5, f"{Z_ASSY_TOP:.2f} assy H", fs=8)
    _dim_h(ax, -hw, hw, -3.5, f"{OUTER_W:.2f} overall W  (v1 30.20 → +{OUTER_W - V1_OUTER_W:.1f})", side=-1, fs=7.5)
    ax.set_xlim(-hw - 14, hw + 14)
    ax.set_ylim(-7, Z_ASSY_TOP + 8)
    ax.axis("off")

    # SECTION through snap — THE KEY FIT DRAWING
    ax = fig.add_subplot(gs[1, 0])
    ax.set_title("SECTION A–A  snap channel  (PCB clearance + hinge raise)", fontsize=10, pad=6)
    ax.set_aspect("equal")
    y0 = -OUTER_W / 2
    ax.add_patch(Rectangle((y0, 0), OUTER_W, BOTTOM_OUTER_H, facecolor=FILL_BOT, edgecolor=INK, lw=1.0))
    ax.add_patch(Rectangle((y0 + WALL, Z_FLOOR), INNER_W, BOTTOM_INNER_H, facecolor="#f4f1ea", edgecolor="none"))
    ax.add_patch(Rectangle((y0, Z_RIM), OUTER_W, LID_OUTER_H, facecolor=FILL_LID, edgecolor=INK, lw=1.0))
    ax.add_patch(Rectangle((y0 + WALL, Z_RIM), INNER_W, LID_INNER_H, facecolor="#eef3f8", edgecolor="none"))
    # channel highlight
    ax.add_patch(Rectangle(
        (PCB_W / 2, Z_FLOOR), INNER_W / 2 - PCB_W / 2, BOTTOM_INNER_H + LID_INNER_H,
        facecolor=FILL_CH, edgecolor=FIX_C, lw=0.6, alpha=0.55,
    ))
    ax.add_patch(Rectangle(
        (-INNER_W / 2, Z_FLOOR), INNER_W / 2 - PCB_W / 2, BOTTOM_INNER_H + LID_INNER_H,
        facecolor=FILL_CH, edgecolor=FIX_C, lw=0.6, alpha=0.55,
    ))
    ax.add_patch(Rectangle((-PCB_W / 2, Z_PCB_BOT), PCB_W, PCB_T, facecolor=FILL_PCB, edgecolor=INK, lw=0.5))
    for s in (1, -1):
        yc = s * PIN_ROW_C / 2
        ax.add_patch(Rectangle((yc - 1.27, Z_PCB_TOP), 2.54, HEADER_H, facecolor="#222", edgecolor="none"))
        ax.add_patch(Rectangle((yc - 0.3, Z_PCB_BOT - PIN_STICK), 0.6, PIN_STICK, facecolor=FILL_PIN, edgecolor="none"))
    # pocket rail ghost
    ax.plot(
        [PCB_POCKET_W / 2, PCB_POCKET_W / 2], [Z_FLOOR, Z_PCB_TOP + STOP_H_EXTRA],
        color=OK_C, lw=1.2, ls="--",
    )
    # window + hook on +Y
    inner_y = INNER_W / 2
    finger_inner = FINGER_INNER
    z_cwin = 0.5 * (Z_BARB_TIP + Z_BARB_CATCH) + 0.2
    ax.add_patch(Rectangle(
        (inner_y, z_cwin - WIN_H / 2), WALL + 0.2, WIN_H,
        facecolor="#111", edgecolor=INK, lw=0.4,
    ))
    t, L, b, root = HOOK_T, HOOK_L, HOOK_BARB, HOOK_ROOT_ABOVE_RIM
    hy = [
        (inner_y - t, Z_RIM + root),
        (inner_y - t, Z_RIM - L),
        (inner_y, Z_RIM - L),
        (inner_y + b, Z_RIM - L + b),
        (inner_y, Z_RIM - L + 2 * b),
        (inner_y, Z_RIM + root),
    ]
    ax.add_patch(Polygon(hy, closed=True, facecolor=FIX_C, edgecolor=INK, lw=0.6))
    # clearance dimension PCB edge → finger inner
    ax.annotate(
        "", xy=(finger_inner, Z_PCB_TOP + 0.3), xytext=(PCB_W / 2, Z_PCB_TOP + 0.3),
        arrowprops=dict(arrowstyle="<->", color=OK_C, lw=1.1),
    )
    ax.text(
        (finger_inner + PCB_W / 2) / 2, Z_PCB_TOP + 1.6,
        f"{SNAP_CLEAR:.2f} mm\nPCB→finger",
        ha="center", va="bottom", fontsize=7, color=OK_C, fontweight="bold",
    )
    # hinge above header
    ax.plot([-hw, hw], [Z_HEADER_TOP, Z_HEADER_TOP], color="#888", lw=0.5, ls=":")
    ax.plot([-hw, hw], [Z_HOOK_ROOT, Z_HOOK_ROOT], color=FIX_C, lw=0.7, ls="--")
    ax.text(
        -hw + 1, Z_HOOK_ROOT + 0.3,
        f"hinge Z={Z_HOOK_ROOT:.1f}  (+{Z_HOOK_ROOT - Z_HEADER_TOP:.1f} above header)",
        fontsize=6.5, color=FIX_C, va="bottom",
    )
    ax.annotate(
        "diamond barb 45°/45°\n(channel, clears board)",
        xy=(inner_y + b, Z_RIM - L + b),
        xytext=(inner_y + 5.5, Z_RIM - L - 3),
        fontsize=6.5, color=FIX_C,
        arrowprops=dict(arrowstyle="->", color=FIX_C, lw=0.7),
    )
    ax.annotate(
        f"window {WIN_W:.1f}×{WIN_H:.1f}\n(v1 {V1_WIN_W:.1f}×{V1_WIN_H:.1f})",
        xy=(inner_y + WALL / 2, z_cwin),
        xytext=(inner_y + 5.5, z_cwin + 5.5),
        fontsize=6.5, color=NOTE_C,
        arrowprops=dict(arrowstyle="->", color=NOTE_C, lw=0.7),
    )
    ax.text(
        0, -2.2,
        f"v1 PCB→finger = {V1_PCB_TO_FINGER:.2f} mm (OVERLAP)  →  v2 = {SNAP_CLEAR:.2f} mm clear",
        ha="center", fontsize=7.5, color=ASSUMED_C, fontweight="bold",
    )
    ax.set_xlim(-hw - 4, hw + 18)
    ax.set_ylim(-4, Z_ASSY_TOP + 3)
    ax.set_xlabel("Y mm", fontsize=7)
    ax.set_ylabel("Z mm", fontsize=7)
    ax.tick_params(labelsize=6)

    # Z / delta callouts
    ax = fig.add_subplot(gs[1, 1])
    ax.set_title("v2 fit numbers  &  delta vs v1", fontsize=10, pad=6)
    ax.axis("off")
    lines = [
        ("PCB → snap finger inner", f"{SNAP_CLEAR:.2f} mm", f"v1 {V1_PCB_TO_FINGER:.2f} (overlap)"),
        ("Snap hinge vs header top", f"+{Z_HOOK_ROOT - Z_HEADER_TOP:.2f} mm", "v1 hinge at rim (below header)"),
        ("Barb catch vs board top", f"{Z_PCB_TOP - Z_BARB_CATCH:.2f} mm below", "engages after stem clears board"),
        ("Barb tip Z / catch Z", f"{Z_BARB_TIP:.2f} / {Z_BARB_CATCH:.2f}", f"HOOK_L {HOOK_L:.1f} (v1 {V1_HOOK_L:.1f})"),
        ("XY pocket clear", f"{XY_CLEAR:.2f} mm", f"v1 {V1_XY_CLEAR:.2f}  (+{XY_CLEAR - V1_XY_CLEAR:.2f})"),
        ("Inner W / Outer W", f"{INNER_W:.2f} / {OUTER_W:.2f}", f"v1 {V1_INNER_W:.2f} / {V1_OUTER_W:.2f}"),
        ("Base windows", f"{WIN_W:.1f} × {WIN_H:.1f}", f"v1 {V1_WIN_W:.1f} × {V1_WIN_H:.1f}"),
        ("Base changed?", "YES", "wider cavity + corner stops + bigger windows"),
        ("USB / BOOT / EN", "USB-C only", "no BOOT/EN holes; LED membrane kept"),
        ("Snaps / screws", "4 diamond / 0", "K1C, PETG, 45°/45°, no supports"),
    ]
    y = 0.93
    ax.text(0.02, y, "Item", fontsize=8, fontweight="bold", transform=ax.transAxes, color=INK)
    ax.text(0.42, y, "v2", fontsize=8, fontweight="bold", transform=ax.transAxes, color=FIX_C)
    ax.text(0.68, y, "vs v1", fontsize=8, fontweight="bold", transform=ax.transAxes, color=NOTE_C)
    y -= 0.08
    for a, b, c in lines:
        ax.text(0.02, y, a, fontsize=7.2, transform=ax.transAxes, color=INK, va="top")
        ax.text(0.42, y, b, fontsize=7.2, transform=ax.transAxes, color=FIX_C, va="top")
        ax.text(0.68, y, c, fontsize=6.8, transform=ax.transAxes, color=NOTE_C, va="top")
        y -= 0.078
    _banner(fig, "ASSUMED board envelope unchanged from v1. Same 3 caliper flags. Fit fix = snap channel + raised hinge + wider windows + +0.4 mm pocket.")


def draw_page2(fig, bottom_info, lid_info):
    fig.clear()
    fig.suptitle(
        f"{TITLE}  {REV}   —   print  ·  BOM  ·  assembly  ·  flags",
        fontsize=12, fontweight="bold", color=INK, y=0.98,
    )
    gs = fig.add_gridspec(
        2, 2, left=0.05, right=0.98, top=0.92, bottom=0.07,
        wspace=0.18, hspace=0.32,
    )

    ax = fig.add_subplot(gs[0, :])
    ax.set_title("What changed from v1 (fit fix)", fontsize=10)
    ax.axis("off")
    col_labels = ["Item", "v1", "v2", "Why"]
    cells = [
        ["PCB→finger clear", f"{V1_PCB_TO_FINGER:.2f} mm (overlap)", f"{SNAP_CLEAR:.2f} mm", "Tabs can flex past board"],
        ["Snap hinge", "at parting rim", f"+{HOOK_ROOT_ABOVE_RIM:.1f} mm into lid", "Flex starts above headers"],
        ["XY pocket clear", f"{V1_XY_CLEAR:.2f}", f"{XY_CLEAR:.2f}", "Board drops easily, still locates"],
        ["Inner / outer W", f"{V1_INNER_W:.1f} / {V1_OUTER_W:.1f}", f"{INNER_W:.1f} / {OUTER_W:.1f}", "Room for flex channel"],
        ["Hook L / barb", f"{V1_HOOK_L:.1f} / {V1_HOOK_BARB:.2f}", f"{HOOK_L:.1f} / {HOOK_BARB:.2f}", "Align with taller window"],
        ["Base window", f"{V1_WIN_W:.1f}×{V1_WIN_H:.1f}", f"{WIN_W:.1f}×{WIN_H:.1f}", "Lock with extra travel"],
        ["PCB locate", "posts only", "posts + corner stops", "Wide cavity still registers board"],
        ["USB / LED / pins", "USB-C, LED mem, enclosed", "unchanged", "Locked openings kept"],
    ]
    table = ax.table(cellText=cells, colLabels=col_labels, loc="upper center", cellLoc="left")
    table.auto_set_font_size(False)
    table.set_fontsize(7.2)
    table.scale(1.0, 1.55)
    for (r, c), cell in table.get_celld().items():
        cell.set_edgecolor("#cccccc")
        if r == 0:
            cell.set_facecolor("#1a1a1a")
            cell.set_text_props(color="white", fontweight="bold")
        elif r % 2 == 0:
            cell.set_facecolor("#f4f1ea")
        if c == 2 and r > 0:
            cell.set_text_props(color=FIX_C)

    ax = fig.add_subplot(gs[1, 0])
    ax.set_title("Print orientation  (no supports)  K1C 0.20 / 0.4 nozzle", fontsize=10)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.add_patch(Rectangle((0, 0), 24, 2.2, facecolor="#888", edgecolor=INK, lw=0.4))
    ax.text(12, -1.4, "bed", ha="center", fontsize=7, color="#666")
    ax.add_patch(Rectangle((4, 2.2), 16, 8, facecolor=FILL_BOT, edgecolor=INK, lw=1.0))
    ax.text(12, 6.2, "BOTTOM\nfloor down\ncavity up", ha="center", va="center", fontsize=8)
    ax.text(12, 1.0, f"windows bridge (~{WIN_W:.0f} mm) OK", ha="center", fontsize=6, color=NOTE_C)
    ax.add_patch(Rectangle((30, 0), 24, 2.2, facecolor="#888", edgecolor=INK, lw=0.4))
    ax.add_patch(Rectangle((34, 2.2), 16, 4.2, facecolor=FILL_LID, edgecolor=INK, lw=1.0))
    for hx in (39, 46):
        ax.add_patch(Polygon(
            [(hx, 6.4), (hx, 11.0), (hx + 1.6, 10.0), (hx, 9.0)],
            closed=True, facecolor=FIX_C, edgecolor=INK, lw=0.4,
        ))
    ax.text(42, 4.2, "LID\nouter face down\nhooks UP", ha="center", va="center", fontsize=8)
    ax.text(42, 12.0, "45° barb on top of print", ha="center", fontsize=6.5, color=FIX_C)
    ax.set_xlim(-2, 58)
    ax.set_ylim(-4, 15)
    ax.text(0, 14.0, "PETG preferred for snaps. 4–5 perimeters, 20% infill, no supports.", fontsize=7.5, color=NOTE_C)
    ax.text(
        0, -3.2,
        f"STL bbox bottom {bottom_info['bbox_mm']}   lid {lid_info['bbox_mm']}   both << {K1C_XY:.0f} mm",
        fontsize=7, color=OK_C,
    )

    ax = fig.add_subplot(gs[1, 1])
    ax.set_title("BOM  ·  assembly  ·  caliper flags", fontsize=10)
    ax.axis("off")
    bom = (
        "BOM (no fasteners)\n"
        f"  1  bottom.stl     PETG/PLA   {bottom_info['bbox_mm'][0]:.1f}×{bottom_info['bbox_mm'][1]:.1f}×{bottom_info['bbox_mm'][2]:.1f} mm\n"
        f"  1  lid.stl        PETG/PLA   {lid_info['bbox_mm'][0]:.1f}×{lid_info['bbox_mm'][1]:.1f}×{lid_info['bbox_mm'][2]:.1f} mm\n"
        "  0  screws         snap-fit only\n"
        "  —  NodeMCU ESP-32S V1.1 + USB-C cable  (existing)\n"
        "\n"
        "Assembly (tabs clear board, then click)\n"
        "  1. Seat board in bottom on 4 posts; corner stops locate XY.\n"
        "     USB at U-slot; cable can stay plugged in.\n"
        "  2. Lower lid: snap fingers travel in the side channels,\n"
        "     clearing PCB / headers / USB / BOOT / EN.\n"
        "  3. Four clicks as barbs enter the base windows.\n"
        "  4. Open: pinch long-side windows, lift lid.\n"
        "\n"
        "CALIPER FLAGS (same as v1 — envelope unchanged)\n"
        "  1. PCB L × W  (design 48.5 × 25.4 +0.8 pocket)\n"
        "  2. Pin stick-out  (design 6.0)\n"
        "  3. USB-C overmold W × H seated  (design 12.4 × 7.5)\n"
    )
    ax.text(
        0.0, 0.98, bom, va="top", ha="left", fontsize=7.2,
        fontfamily="monospace", color=INK, transform=ax.transAxes,
    )
    _banner(fig, "v2: snaps clear board then lock. Base YES changed. No screws. USB-C only. LED membrane. Pins enclosed.")


def write_drawings(bottom_info, lid_info):
    pngs = []
    pdf_path = OUT_DRAW / "esp32-smoker-case-v2.pdf"
    with PdfPages(pdf_path) as pdf:
        fig = plt.figure(figsize=(17, 11), dpi=140)
        draw_page1(fig)
        fig.savefig(OUT_DRAW / "sheet1-overall-snap-section.png", dpi=160, facecolor="white")
        pdf.savefig(fig, facecolor="white")
        pngs.append("sheet1-overall-snap-section.png")
        draw_page2(fig, bottom_info, lid_info)
        fig.savefig(OUT_DRAW / "sheet2-delta-print-bom.png", dpi=160, facecolor="white")
        pdf.savefig(fig, facecolor="white")
        pngs.append("sheet2-delta-print-bom.png")
        plt.close(fig)
    return pdf_path, pngs


def write_readme(bottom_info, lid_info):
    pcb_to_finger = FINGER_INNER - PCB_W / 2.0
    path = ROOT / "README.md"
    path.write_text(
        f"""# ESP-32 smoker relay snap case {REV}

Two-piece snap-fit clamshell for Brandon’s **NodeMCU ESP-32S V1.1** smoker relay (USB-C, dual 19-pin headers). **No screws.** USB-C cable stays plugged in. Pins fully enclosed. No BOOT/EN holes.

## What changed (v1 → v2 fit fix)

Printed v1 cover snap tabs could **not** slide past the board and lock. Root cause: v1 snap fingers sat **inside** the PCB outline (PCB→finger = **{V1_PCB_TO_FINGER:.2f} mm**, i.e. overlap). Diamond barbs / stems hit the PCB edge, header plastic, USB jack, or BOOT/EN before they could flex into the base windows.

| Fix | v1 | v2 |
|---|---:|---:|
| PCB → snap finger inner | {V1_PCB_TO_FINGER:.2f} mm (overlap) | **{pcb_to_finger:.2f} mm** clear |
| Snap hinge | at parting rim | **+{HOOK_ROOT_ABOVE_RIM:.1f} mm** into lid (above header tops) |
| XY PCB pocket clear | {V1_XY_CLEAR:.2f} | **{XY_CLEAR:.2f}** (+{XY_CLEAR - V1_XY_CLEAR:.2f}) |
| Inner W / Outer W | {V1_INNER_W:.1f} / {V1_OUTER_W:.1f} | **{INNER_W:.1f} / {OUTER_W:.1f}** |
| Hook L / barb | {V1_HOOK_L:.1f} / {V1_HOOK_BARB:.2f} | **{HOOK_L:.1f} / {HOOK_BARB:.2f}** |
| Base windows | {V1_WIN_W:.1f}×{V1_WIN_H:.1f} | **{WIN_W:.1f}×{WIN_H:.1f}** |
| Base changed? | — | **YES** (wider cavity, corner stops, bigger windows) |

Kept: dual 19-pin enclosed, USB-C overmold opening only, LED membrane, 4 snaps, no screws, K1C/PETG 45°/45° barbs, **same ASSUMED board dims + caliper flags as v1**.

Regenerate:

```bash
python3 /workspace/esp32-smoker-case-v2/generate_package.py
```

## What prints

| STL | Print orientation | BBox mm | Notes |
|---|---|---|---|
| `stl/bottom.stl` | floor on bed, cavity up | {bottom_info['bbox_mm'][0]:.1f} × {bottom_info['bbox_mm'][1]:.1f} × {bottom_info['bbox_mm'][2]:.1f} | 4 wider snap windows; corner stops |
| `stl/lid.stl` | **outer face on bed**, hooks up | {lid_info['bbox_mm'][0]:.1f} × {lid_info['bbox_mm'][1]:.1f} × {lid_info['bbox_mm'][2]:.1f} | hooks in flex channel, raised hinge |

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
| PCB L × W × T | {PCB_L:.2f} × {PCB_W:.2f} × {PCB_T:.2f} | **ASSUMED** |
| Pin pitch / count / span | {PIN_PITCH:.2f} / {PIN_N} / {PIN_SPAN:.2f} | KNOWN |
| Pin row centers | {PIN_ROW_C:.2f} (0.9″) | **ASSUMED** |
| Header plastic H | {HEADER_H:.2f} | **ASSUMED** |
| Pin stick-out | {PIN_STICK:.2f} | **ASSUMED FLAG** |
| Shield-can H | {SHIELD_H:.2f} | **ASSUMED FLAG** |
| USB-C overmold | {USB_OVER_W:.2f} × {USB_OVER_H:.2f} | H **ASSUMED FLAG** |

Internal PCB pocket: outline +{XY_CLEAR:.2f} mm. Snap flex channel adds long-side width so finger inner face is **{pcb_to_finger:.2f} mm** outside the PCB edge. Pins hang in an {PIN_WELL:.1f} mm well and do **not** poke out.

## USB-C / LED / snaps

- **USB-C opening only**: {USB_OPEN_W:.1f} × {USB_OPEN_H:.1f} mm rounded (R{USB_OPEN_R:.1f}), 0.8 mm lead-in, split across parting line.
- **LED**: 5 mm Ø × 0.6 mm membrane over EN-side red LED (no through-hole).
- **Snaps**: 4 diamond-barb hooks, {HOOK_W:.0f} mm wide, {HOOK_BARB:.2f} mm barb, 45°/45°, hinge {HOOK_ROOT_ABOVE_RIM:.1f} mm above rim. Through-windows for fingernail release.

## Clearance report (for parent → Rhea)

- **PCB edge → snap finger inner face**: **{pcb_to_finger:.2f} mm** (v1 was {V1_PCB_TO_FINGER:.2f} mm overlap)
- **Snap hinge vs header top**: hinge at Z={Z_HOOK_ROOT:.2f}, header top Z={Z_HEADER_TOP:.2f} → **+{Z_HOOK_ROOT - Z_HEADER_TOP:.2f} mm** above headers
- **Barb catch vs board top**: catch Z={Z_BARB_CATCH:.2f}, board top Z={Z_PCB_TOP:.2f} → barb locks **{Z_PCB_TOP - Z_BARB_CATCH:.2f} mm below** board top (after stem has cleared laterally)
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

v1 left untouched at `/workspace/esp32-smoker-case-v1/`.
""",
        encoding="utf-8",
    )
    return path


def write_bom(bottom_info, lid_info):
    bom_path = ROOT / "bom.csv"
    cut_path = ROOT / "print_list.csv"
    rows = [
        {
            "item": "P1",
            "qty": 1,
            "part": "bottom.stl",
            "material": "PETG (preferred) or PLA",
            "bbox_mm": f"{bottom_info['bbox_mm'][0]:.1f}x{bottom_info['bbox_mm'][1]:.1f}x{bottom_info['bbox_mm'][2]:.1f}",
            "process": "FDM 0.20/0.4, 5 perimeters, 20% infill, no supports, floor down",
            "notes": "v2: wider cavity, corner stops, larger snap windows; USB U-slot; pins enclosed",
        },
        {
            "item": "P2",
            "qty": 1,
            "part": "lid.stl",
            "material": "PETG (preferred) or PLA",
            "bbox_mm": f"{lid_info['bbox_mm'][0]:.1f}x{lid_info['bbox_mm'][1]:.1f}x{lid_info['bbox_mm'][2]:.1f}",
            "process": "FDM 0.20/0.4, 5 perimeters, 20% infill, no supports, OUTER FACE down",
            "notes": "v2: snaps in flex channel, raised hinge; USB inverted-U; LED 0.6 mm membrane",
        },
        {
            "item": "H1",
            "qty": 0,
            "part": "screws",
            "material": "—",
            "bbox_mm": "—",
            "process": "none",
            "notes": "snap-fit only; do not add screws",
        },
        {
            "item": "A1",
            "qty": 1,
            "part": "NodeMCU ESP-32S V1.1 (existing)",
            "material": "—",
            "bbox_mm": f"{PCB_L:.1f}x{PCB_W:.1f} ASSUMED",
            "process": "drop in",
            "notes": "USB-C cable stays plugged in",
        },
    ]
    fields = list(rows[0].keys())
    for path, extra in ((bom_path, rows), (cut_path, rows[:2])):
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(extra)
    return bom_path, cut_path


def main():
    # Sanity: clearance numbers
    pcb_to_finger = FINGER_INNER - PCB_W / 2.0
    assert pcb_to_finger >= 1.2 - 1e-6, pcb_to_finger
    assert Z_HOOK_ROOT >= Z_HEADER_TOP - 1e-6, (Z_HOOK_ROOT, Z_HEADER_TOP)
    print(f"v2 clearances: PCB→finger={pcb_to_finger:.2f}  hinge−header={Z_HOOK_ROOT - Z_HEADER_TOP:.2f}")
    print(f"  OUTER {OUTER_L:.2f}×{OUTER_W:.2f}  INNER {INNER_L:.2f}×{INNER_W:.2f}  pocket {PCB_POCKET_L:.2f}×{PCB_POCKET_W:.2f}")

    print("Building bottom…")
    bottom = build_bottom()
    print("Building lid…")
    lid = build_lid()

    print("Exporting STEP (assembled orientation)…")
    cq.exporters.export(bottom, str(OUT_STEP / "bottom.step"))
    cq.exporters.export(lid, str(OUT_STEP / "lid.step"))

    print("Exporting STL (print orientation) + 3MF…")
    bottom_info = export_stl(bottom, OUT_STL / "bottom.stl")
    lid_info = export_stl(lid, OUT_STL / "lid.stl", print_rotate="lid")
    print("  bottom", bottom_info)
    print("  lid   ", lid_info)
    if not bottom_info["k1c_ok"] or not lid_info["k1c_ok"]:
        raise SystemExit("K1C XY limit exceeded")

    dims = dims_dict(bottom_info, lid_info)
    (ROOT / "dims.json").write_text(json.dumps(dims, indent=2), encoding="utf-8")

    print("Drawings…")
    pdf_path, pngs = write_drawings(bottom_info, lid_info)
    print("  ", pdf_path, pngs)
    write_readme(bottom_info, lid_info)
    write_bom(bottom_info, lid_info)
    print("Done.", ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
