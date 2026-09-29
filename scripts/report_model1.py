"""Stage 4 figure: Model 1 against its references, its confusions, what it looks at.

    python scripts/report_model1.py      # after scripts/train.py

Reads reports/model1_metrics.json and writes reports/stage4/fig_model1.png.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import spike_cluster as sc  # noqa: E402  (shared figure style)

METRICS = REPO / "reports" / "model1_metrics.json"
OUT = REPO / "reports" / "stage4"
BLUE = "#2a78d6"
PALE = "#c3c2b7"
NAMES = {"oil_gas": "oil & gas", "heavy_industry": "heavy industry", "mining": "mining"}


def main() -> int:
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap

    m = json.loads(METRICS.read_text(encoding="utf-8"))
    refs = m["references"]
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.8), facecolor=sc.SURFACE,
                             gridspec_kw={"width_ratios": [1.0, 1.0, 1.2]})

    # 1. Balanced accuracy against the references.
    ax = axes[0]
    bars = [("always the\nmajority class", refs["majority_class"]["balanced_accuracy"], PALE),
            ("shuffled labels\n(mean of 100)", refs["permutation_test"]["null_mean"], PALE),
            ("location only\n(lat / lon)", refs["location_only_lat_lon"]["balanced_accuracy"],
             PALE),
            ("Model 1\n(fingerprint)", m["block_cv"]["balanced_accuracy"], BLUE)]
    ax.bar(range(4), [b[1] * 100 for b in bars], color=[b[2] for b in bars], width=0.6)
    for i, (_, v, _) in enumerate(bars):
        ax.text(i, v * 100 + 1.2, f"{v:.0%}", ha="center", fontsize=9, color=sc.INK)
    ax.set_xticks(range(4), [b[0] for b in bars], fontsize=8, color=sc.INK2)
    ax.set_ylim(0, 75)
    ax.set_ylabel("balanced accuracy, block CV (%)", fontsize=8, color=sc.INK2)
    sc._style(ax, "Better than geography, better than chance")

    # 2. Confusion matrix, rows normalised.
    ax = axes[1]
    classes = m["block_cv"]["confusion"]["rows_true_cols_pred"]
    cm = np.array(m["block_cv"]["confusion"]["matrix"], dtype=float)
    share = cm / cm.sum(axis=1, keepdims=True)
    ramp = LinearSegmentedColormap.from_list("blue", [sc.SURFACE, "#9ec5f4", BLUE, "#0d366b"])
    ax.imshow(share, cmap=ramp, vmin=0, vmax=1)
    for i in range(len(classes)):
        for j in range(len(classes)):
            ax.text(j, i, f"{share[i, j]:.0%}\n({int(cm[i, j])})", ha="center", va="center",
                    fontsize=9, color=sc.SURFACE if share[i, j] > 0.55 else sc.INK)
    labels = [NAMES.get(c, c) for c in classes]
    ax.set_xticks(range(len(classes)), labels, fontsize=8, color=sc.INK2)
    ax.set_yticks(range(len(classes)), labels, fontsize=8, color=sc.INK2)
    ax.set_xlabel("predicted", fontsize=8, color=sc.INK2)
    ax.set_ylabel("weak label (OSM / power plants)", fontsize=8, color=sc.INK2)
    sc._style(ax, "Where it errs: mining vs heavy industry")
    for side in ("left", "bottom"):
        ax.spines[side].set_visible(False)

    # 3. What it looks at: mean |SHAP|.
    ax = axes[2]
    imp = list(m["importance_mean_abs_shap"]["overall"].items())[:10][::-1]
    ax.barh(range(len(imp)), [v for _, v in imp], color=BLUE, height=0.6)
    ax.set_yticks(range(len(imp)), [k for k, _ in imp], fontsize=8, color=sc.INK2)
    ax.set_xlabel("mean |SHAP| (log-odds)", fontsize=8, color=sc.INK2)
    sc._style(ax, "What it looks at: how a source burns, never where")

    fig.suptitle(f"Model 1 on {m['evaluation']['trained_on']} labelled registry sources, "
                 f"{m['evaluation']['scheme']}", x=0.01, ha="left", fontsize=11, color=sc.INK)
    fig.tight_layout()
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / "fig_model1.png", dpi=130, facecolor=sc.SURFACE)
    plt.close(fig)
    print("wrote", OUT / "fig_model1.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
