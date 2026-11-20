# README workflow figure: python docs/workflow_figure.py -> docs/pipeline_v5.svg (needs matplotlib)
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Circle

for f in Path("/usr/share/fonts/opentype/inter").glob("Inter-*.otf"):   # falls back to DejaVu Sans if absent
    font_manager.fontManager.addfont(str(f))
plt.rcParams.update({"font.family": ["Inter", "DejaVu Sans"], "svg.fonttype": "path"})

KIND = {  # fill, edge, title colour
    "you": ("#FFF1D6", "#E0952B", "#7A4A00"),
    "agent": ("#E6EFFB", "#3D7BD6", "#1B4A8F"),
    "ctrl": ("#EEF0F3", "#8A9099", "#343A42"),
}
STEPS = [  # (kind, title, body, tracks that use this step: "" = all)
    ("you", "Spec", "/research-spec\nagree on 00_spec.md", ""),
    ("ctrl", "Setup", "clone without remote,\nper-run venv", ""),
    ("agent", "Recon", "repo scan; literature\nsearch for A B C", ""),
    ("agent", "Ideas / Claims", "A B C: 1:1 debates → rank\nD: paper claims, you pick", ""),
    ("agent", "Pilot", "write scoring code,\nlock it, smoke run", "A B D"),
    ("you", "Check", "you check the\nscoring code", "A B D"),
    ("agent", "Experiments", "A compare · B optimize\nC prove ↔ refute\nD reproduce claims", ""),
    ("agent", "Audit", "other-vendor models\ncheck the claims", ""),
    ("agent", "Report", "written from results,\nthen fact-checked", ""),
    ("you", "Approve", "squash commit,\npush to private repo", ""),
]
PER_ROW, BW, BH, GAP, PAD = 5, 1.62, 1.05, 0.34, 0.14
W = 2 * PAD + PER_ROW * BW + (PER_ROW - 1) * GAP
ROW_GAP = 0.5
H = PAD + 2 * BH + ROW_GAP + 0.5
fig = plt.figure(figsize=(W, H))
ax = fig.add_axes([0, 0, 1, 1])
ax.set_xlim(0, W)
ax.set_ylim(0, H)
ax.axis("off")
GREY = "#6B7280"


def arrow(p, q):
    ax.add_patch(FancyArrowPatch(p, q, arrowstyle="-|>", mutation_scale=12, lw=1.4, color=GREY, shrinkA=0, shrinkB=0))


pos = []
for i, (kind, title, body, tracks) in enumerate(STEPS):
    row, col = divmod(i, PER_ROW)
    x, top = PAD + col * (BW + GAP), H - PAD - row * (BH + ROW_GAP)
    pos.append((x, top))
    fill, edge, tcol = KIND[kind]
    ax.add_patch(FancyBboxPatch((x, top - BH), BW, BH, boxstyle="round,pad=0,rounding_size=0.09",
                                fc=fill, ec=edge, lw=1.4))
    ax.add_patch(Circle((x + 0.13, top - 0.13), 0.085, fc=edge, ec="none"))
    ax.text(x + 0.13, top - 0.132, str(i + 1), fontsize=7.5, weight="bold", color="white", ha="center", va="center")
    ax.text(x + BW / 2, top - 0.31, title, fontsize=11, weight="bold", color=tcol, ha="center", va="center")
    ax.text(x + BW / 2, top - 0.70, body, fontsize=8.6, color="#2F343B", ha="center", va="center", linespacing=1.4)
    if tracks:
        ax.text(x + BW - 0.1, top - 0.13, tracks, fontsize=7.5, weight="bold", color=edge, ha="right", va="center")
    if col > 0:
        arrow((x - GAP + 0.04, top - BH / 2), (x - 0.04, top - BH / 2))

# wrap from the end of row 1 to the start of row 2
(x5, t5), (x6, t6) = pos[PER_ROW - 1], pos[PER_ROW]
ymid = t5 - BH - ROW_GAP / 2
ax.plot([x5 + BW / 2, x5 + BW / 2, x6 + BW / 2], [t5 - BH - 0.04, ymid, ymid], color=GREY, lw=1.4)
arrow((x6 + BW / 2, ymid), (x6 + BW / 2, t6 + 0.04))

# legend
for i, (k, name) in enumerate([("you", "you"), ("agent", "LLM agents"), ("ctrl", "controller")]):
    fill, edge, _ = KIND[k]
    x = PAD + i * 1.15
    ax.add_patch(FancyBboxPatch((x, 0.13), 0.22, 0.15, boxstyle="round,pad=0,rounding_size=0.03",
                                fc=fill, ec=edge, lw=1.1))
    ax.text(x + 0.3, 0.205, name, fontsize=8.6, color="#4B5563", va="center")

fig.savefig(Path(__file__).parent / "pipeline_v5.svg", facecolor="white")
