"""Risk: how much a fire matters, as three factors that must all hold.

    Risk = 100 x H^0.40 x E^0.35 x V^0.25,   each of H, E, V in [0.05, 1]

- H, hazard: how violent the fire is (``score``)
- E, exposure: who and what is near it -- people, and people downwind (``population``,
  ``firewatch.ingest.wind``), and other critical assets
- V, vulnerability: what is burning, from a static asset register (``assets``)

Multiplicative on purpose: a huge fire in an empty desert beside nothing valuable
must score low, and an additive score would hand it a high one on hazard alone.
Every score ships with its decomposition and the raw values behind each term.
"""
