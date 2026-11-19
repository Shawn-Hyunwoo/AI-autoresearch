# README workflow figure: python docs/workflow_figure.py -> docs/workflow.svg (needs matplotlib)
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

for f in Path("/usr/share/fonts/opentype/inter").glob("Inter-*.otf"):   # falls back to DejaVu Sans if absent
    font_manager.fontManager.addfont(str(f))
plt.rcParams.update({"font.family": ["Inter", "DejaVu Sans"], "svg.fonttype": "path"})

KIND = {  # fill, edge, title colour
    "human": ("#FFF3DF", "#E0952B", "#8A5300"),
    "agent": ("#EAF1FB", "#3D7BD6", "#1E4E95"),
    "ctrl": ("#F1F2F4", "#8A9099", "#3C4149"),
}
GREY = "#6B7280"
W, H = 112, 134
fig = plt.figure(figsize=(W / 12.5, H / 12.5))
ax = fig.add_axes([0, 0, 1, 1])
ax.set_xlim(0, W)
ax.set_ylim(0, H)
ax.axis("off")
L, R = 18, 98
CX = (L + R) / 2


def box(x, top, w, h, kind, title, body="", tag=""):
    fill, edge, tcol = KIND[kind]
    ax.add_patch(FancyBboxPatch((x, top - h), w, h, boxstyle="round,pad=0,rounding_size=1.6",
                                fc=fill, ec=edge, lw=1.3, ls="--" if kind == "human" else "-"))
    ty = top - 2.7 if body else top - h / 2
    ax.text(x + 2, ty, title, fontsize=10.5, weight="semibold", color=tcol, va="center")
    if tag:
        ax.text(x + w - 2, ty, tag, fontsize=7.8, color=GREY, va="center", ha="right", style="italic")
    if body:
        ax.text(x + 2, top - 4.8, body, fontsize=8.6, color="#30343B", va="top", linespacing=1.45)


def arrow(p, q, color=GREY, lw=1.2):
    ax.add_patch(FancyArrowPatch(p, q, arrowstyle="-|>", mutation_scale=11, lw=lw, color=color,
                                 shrinkA=0, shrinkB=0))


def phase(y, text):
    ax.text(15.5, y, text, fontsize=9.5, weight="bold", color="#4B5563", ha="right", va="center")


phase(125, "Phase 0")
box(L, 130, R - L, 10, "human", "/research-spec  →  00_spec.md",
    "Agree on track, metric, budget and model per stage. Missing values are drafted for you.")
arrow((CX, 120), (CX, 116))

phase(111, "Setup")
box(L, 116, R - L, 10, "ctrl", "Isolated workspace",
    "Clone the repo with no remote · work branch · per-run venv · reference answers kept outside")
arrow((CX, 106), (CX, 102))

phase(97, "Phase 1")
box(L, 102, R - L, 10, "agent", "Recon", "Repository scan + literature search: baselines and known approaches",
    tag="read-only")
arrow((CX, 92), (CX, 88))

phase(83, "Phase 1.5")
box(L, 88, R - L, 10, "agent", "Ideation",
    "Several models propose  →  models from other vendors critique  →  selector ranks the candidates",
    tag="read-only")
arrow((CX, 78), (CX, 74))
ax.text(CX + 1.2, 76, "tracks A, B", fontsize=7.8, color=GREY, va="center", style="italic")

phase(64, "Phase 2")
box(L, 74, R - L, 10, "agent", "Pilot",
    "Builder writes the scoring code  →  controller checks it, locks it (SHA-256) and runs a small pilot")
arrow((CX, 64), (CX, 60))
box(L + 12, 60, R - L - 24, 6, "human", "Gate: you review the scoring code and the pilot")

# Phase 3: A and B come through the gate, C skips Phase 2
phase(43, "Phase 3")
gap = 2.5
cw = (R - L - 2 * gap) / 3
tracks = [("A · Utility", "Candidates vs baselines\nper case × regime;\nseeds → mean ± SE"),
          ("B · Optimization", "Edit → alternate best and\ncandidate runs → keep or\nrevert by the median"),
          ("C · Proof", "Prover ↔ refuter from\ndifferent vendors;\ndisagreement → you")]
for i, (t, b) in enumerate(tracks):
    x = L + i * (cw + gap)
    box(x, 50, cw, 14, "agent", t, b)
    if i < 2:
        arrow((CX, 54), (x + cw / 2, 50), color="#9CA3AF", lw=1.0)
    arrow((x + cw / 2, 36), (CX, 32), color="#9CA3AF", lw=1.0)
bx = R + 5
ax.plot([R, bx, bx], [83, 83, 43], color="#9CA3AF", lw=1.0)
arrow((bx, 43), (R, 43), color="#9CA3AF", lw=1.0)
ax.text(bx + 1.3, 63, "track C", fontsize=7.8, color=GREY, va="center", ha="center", rotation=-90, style="italic")

phase(27, "Phase 4")
box(L, 32, R - L, 10, "agent", "Audit → report",
    "Auditors from a vendor other than the main one check the claims; the reporter writes the report",
    tag="read-only")
arrow((CX, 22), (CX, 18))

phase(13, "Finish")
box(L, 18, R - L, 10, "human", "You read the report and approve",
    "One squash commit on branch autoresearch/<run_id> in your repo, pushed only to a private remote")

# legend
items = [("human", "you"), ("agent", "LLM agents"), ("ctrl", "controller: runs and scores every experiment")]
for i, (k, name) in enumerate(items):
    fill, edge, _ = KIND[k]
    x = L + i * 17
    ax.add_patch(FancyBboxPatch((x, 2.4), 3.2, 1.8, boxstyle="round,pad=0,rounding_size=0.5",
                                fc=fill, ec=edge, lw=1.1, ls="--" if k == "human" else "-"))
    ax.text(x + 4.2, 3.3, name, fontsize=8.2, color="#4B5563", va="center")

fig.savefig(Path(__file__).parent / "workflow.svg", facecolor="white")
