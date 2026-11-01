"""The colour hub counter, shipped with Watchtower (built-in counter `colour`).

Copied unchanged from YOLOv26-FRC-Model's `tbavid/` (MIT, see LICENSE here) at commit c957cee:
fms_counter.py (the plugin), hubcount.py (the crossing counter), camprofile.py, and hubmodel.py /
count.py / trackvis.py, which hubcount imports at load time (only the model blend uses them, and no
model is shipped). Fix counter bugs there, then copy the files again and update the commit above.

It counts yellow blobs crossing down into each hub's outline: no model, no torch, no GPU. Cameras,
outlines and the one-ball size are set in /control -> Setup -> Cameras (vision/hubsetup.py).
"""
