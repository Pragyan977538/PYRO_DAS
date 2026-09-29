"""The registry: persistent thermal sources, each with a fingerprint and a baseline.

Built offline from the whole archive in four steps, each a module here:

1. ``cells``       snap VIIRS detections to 375 m cells; gate cells on recurrence
2. ``cluster``     DBSCAN the surviving cells into sources, cap the footprint, and
                   assign every detection (VIIRS and MODIS) to its source
3. ``fingerprint`` thermal and temporal features per source, NaN-safe
4. ``baseline``    median / MAD / p99 per source, keyed instrument|daynight|season

``build`` wires them to the database; ``evaluate`` scores a registry against GIHS,
FIRMS' static flag, the paddy belt and the demo refineries.
"""
