"""Drag a box over a hub opening; prints the ROI in FULL-resolution pixels.
Handles Retina scaling by showing a known-scale resized image in an AUTOSIZE window.

  python pick_roi.py 0            # camera index
  python pick_roi.py frame.png    # still image
"""
import sys

import cv2

src = sys.argv[1]
if src.isdigit():
    cap = cv2.VideoCapture(int(src))
    for _ in range(10):  # let auto-exposure settle
        ok, img = cap.read()
    cap.release()
else:
    img = cv2.imread(src)
s = min(1.0, 1280 / img.shape[1])
small = cv2.resize(img, None, fx=s, fy=s)
cv2.namedWindow("roi", cv2.WINDOW_AUTOSIZE)
x, y, w, h = cv2.selectROI("roi", small, showCrosshair=True)
cv2.destroyAllWindows()
print(f"roi: [{round(x / s)}, {round(y / s)}, {round(w / s)}, {round(h / s)}]   (frame {img.shape[1]}x{img.shape[0]})")
