# Stage 1.5 - generated tables

Year 2023; 1,170,878 VIIRS detections; 29.0% at night.

| Run | Region | Detections | Skip Road A | In blobs >10 km | Widest source |
|---|---|---|---|---|---|
| A | Jamnagar | 943 | 79.4% | 0.0% | 5 km |
| A | Punjab paddy belt | 93,114 | 59.5% | 1.0% | 16.6 km |
| A | Jharia coalfield | 26,229 | 99.1% | 58.4% | 12.6 km |
| B | Jamnagar | 943 | 80.0% | 0.0% | 6.5 km |
| B | Punjab paddy belt | 93,114 | 72.6% | 10.5% | 38.9 km |
| B | Jharia coalfield | 26,229 | 99.2% | 58.4% | 12.6 km |
| C | Jamnagar | 943 | 30.8% | 0.0% | 0.9 km |
| C | Punjab paddy belt | 93,114 | 1.8% | 0.0% | 2.3 km |
| C | Jharia coalfield | 26,229 | 98.0% | 0.0% | 5.1 km |

| Run | Points | Seconds | Peak extra MB |
|---|---|---|---|
| A_snpp | 579,733 | 5.3 | 443 |
| A_ball | 1,170,878 | 12.0 | 1,321 |
| A_kd | 1,170,878 | 10.1 | 1,323 |
| B | 1,170,878 | 11.7 | 1,381 |
| C | 1,170,878 | 2.4 | 271 |

Share of each region's detections within 500 m of a registered source:

| Gate months | Gate days | Sources | Jamnagar | Punjab paddy belt | Jharia coalfield | FIRMS type=2 recall | GIHS recall (active 2021) | Sources on a GIHS site |
|---|---|---|---|---|---|---|---|---|
| 3 | 5 | 742 | 75.5% | 2.0% | 99.0% | 100.0% | 78.0% | 63.2% |
| 3 | 10 | 522 | 68.7% | 1.8% | 98.9% | 99.9% | 71.7% | 79.1% |
| 3 | 20 | 410 | 46.3% | 1.8% | 98.5% | 99.2% | 62.0% | 85.1% |
| 4 | 5 | 613 | 71.6% | 1.9% | 98.8% | 99.9% | 75.3% | 71.8% |
| 4 | 10 | 503 | 68.7% | 1.8% | 98.4% | 99.7% | 70.7% | 79.7% |
| 4 | 20 | 399 | 46.3% | 1.8% | 98.1% | 99.1% | 60.7% | 85.2% |
| 5 | 5 | 525 | 60.7% | 1.9% | 98.4% | 99.6% | 71.2% | 76.4% |
| 5 | 10 | 472 | 60.6% | 1.8% | 98.4% | 99.5% | 68.5% | 80.7% |
| 5 | 20 | 386 | 46.3% | 1.8% | 98.1% | 99.0% | 60.0% | 85.8% |
| 6 | 5 | 461 | 30.9% | 1.8% | 98.1% | 99.3% | 66.6% | 79.6% |
| 6 | 10 | 443 | 30.8% | 1.8% | 98.0% | 99.2% | 65.4% | 80.8% |
| 6 | 20 | 370 | 30.3% | 1.7% | 98.0% | 98.7% | 59.2% | 86.5% |
| 8 | 5 | 347 | 30.6% | 1.7% | 96.7% | 97.4% | 56.1% | 87.0% |
| 8 | 10 | 346 | 30.6% | 1.7% | 96.7% | 97.4% | 56.1% | 87.0% |
| 8 | 20 | 331 | 30.3% | 1.7% | 96.7% | 97.2% | 53.9% | 88.8% |

Run A: 50 of 443 persistent sources sit in a raw cluster made mostly of one-off fires (median contamination 12%).

Run B: 51 of 443 persistent sources sit in a raw cluster made mostly of one-off fires (median contamination 12%).
