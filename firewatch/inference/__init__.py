"""Online inference: every new detection asks one question.

*Is there a known source within 500 m?*

- **No -> Road A** (``road_a``): rules plus physics, each writing its reason.
- **Yes, inside its envelope -> Road B**: infrastructure operating normally.
- **Yes, breaching its own baseline -> Road C** (``anomaly``): a provisional alert on
  one extreme pass, a confirmed alert on two consecutive breaching passes.

Road A sites that keep burning become provisional sources (``promotion``), which
alert on every pass and are never Road B. ``router`` answers the question;
``engine`` runs it all over a batch or a replayed window, in time order.
"""

#: Road numbers as stored in ``detections.road``.
ROAD_A, ROAD_B, ROAD_C = 1, 2, 3
