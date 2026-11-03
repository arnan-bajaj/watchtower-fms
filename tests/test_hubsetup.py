"""Setup -> Cameras on the vision side: a hub's Setup entry -> the colour counter's config, the drop
test's ball-size fit, and the shipped colour counter counting balls that fall into a drawn outline."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "vision"))
import hubsetup  # noqa: E402

BASE = {"counter": "colour", "width": 1920, "height": 1080, "hub": "red"}
BOX = [[500, 300], [780, 300], [760, 420], [520, 420]]


def test_merge_takes_setup_over_the_file():
    file_hub = {**BASE, "source": "2", "roi": [1, 2, 3, 4], "ball_area": 50}
    assert hubsetup.merge(file_hub, None) == file_hub                       # null: the file decides
    cfg = hubsetup.merge(file_hub, {"source": "0", "size": [1280, 720], "outline": BOX, "ball_area": 700})
    assert cfg["source"] == "0" and "roi" not in cfg and cfg["outline"] == BOX
    assert cfg["ball_area"] == 700 and cfg["setup_size"] == [1280, 720]
    assert hubsetup.waiting(cfg) is None
    # a camera picked but no outline yet: nothing of the file's zone is left to count with
    cfg = hubsetup.merge(file_hub, {"source": "0", "size": [0, 0], "outline": [], "ball_area": None})
    assert "roi" not in cfg and "outline" not in cfg and "ball_area" not in cfg
    assert hubsetup.waiting(cfg) == "draw this hub's outline in Setup → Cameras"
    # an outline but no drop test yet: it waits rather than count on a guessed ball size
    cfg = hubsetup.merge(file_hub, {"source": "0", "size": [1280, 720], "outline": BOX, "ball_area": None})
    assert "ball_area" not in hubsetup.fit(cfg, 1280, 720)
    assert hubsetup.waiting(cfg) == "calibrate this hub (drop test) in Setup → Cameras"
    assert hubsetup.waiting({**BASE}) == "no camera: pick one in Setup → Cameras"
    assert hubsetup.waiting({"counter": "mock", "source": "none"}) is None   # mock needs no outline
    assert hubsetup.waiting({**BASE, "source": "0", "setup": "cams.json"}) is None


def test_fit_scales_to_the_camera():
    cfg = hubsetup.merge(BASE, {"source": "0", "size": [1280, 720], "outline": BOX, "ball_area": 400})
    f = hubsetup.fit(cfg, 1920, 1080)                                        # drawn at 720p, camera at 1080p
    assert f["outline"][0] == [750.0, 450.0] and f["ball_area"] == 900.0 and "setup_size" not in f
    assert hubsetup.fit(cfg, 1280, 720)["outline"] == BOX                    # same size: untouched


def _drop(groups, area=700.0, x=640.0, step=9.0):
    """Blobs per frame for balls dropped one group at a time through BOX's top edge (y=300): a group of
    k balls falls as one blob of k balls' area, then is gone inside the hub."""
    frames = []
    for i, k in enumerate(groups):
        cx = x - 60 + 25 * (i % 5)
        frames += [[(cx, y, area * k)] for y in range(int(120 + step), 400, int(step))] + [[] for _ in range(10)]
    return frames


def test_drop_test_fits_the_ball_size():
    import dropfit
    poly = [tuple(p) for p in BOX]
    groups = [1, 1, 3, 1, 2, 1, 1, 4, 1, 1, 2, 1]                           # 19 balls, singles and clumps
    r = dropfit.fit(poly, _drop(groups), sum(groups), 1280, 720)
    assert r["got"] == 19 and 0.6 * 700 <= r["area"] <= 1.25 * 700, r
    # the fitted size then counts a different drop right, clumps included
    other = [2, 1, 1, 3, 1, 1, 1, 2]
    assert dropfit.replay(poly, _drop(other, x=600), r["area"], 1280, 720) == sum(other)
    # singles only: still inside the range where a later clump counts right
    r1 = dropfit.fit(poly, _drop([1] * 12), 12, 1280, 720)
    assert r1["got"] == 12 and 0.6 * 700 <= r1["area"] <= 1.25 * 700, r1
    with pytest.raises(ValueError, match="Nothing crossed"):
        dropfit.fit(poly, [[(100.0, 100.0, 700.0)]] * 50, 5, 1280, 720)


def test_colour_counter_counts_balls_into_the_outline():
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    from counters import load_counter
    cfg = hubsetup.fit(hubsetup.merge({"counter": "colour", "hub": "red"},
                                      {"source": "0", "size": [1280, 720], "outline": BOX, "ball_area": 700}), 1280, 720)
    c = load_counter(cfg)                    # the built-in name, from vision/hubcounter
    t, total = 0.0, 0
    for b in range(6):                       # 6 balls (707 px each) dropping into the hub
        for y in range(120, 400, 9):
            f = np.full((720, 1280, 3), 40, np.uint8)
            cv2.circle(f, (560 + 30 * b, y), 15, (0, 220, 255), -1)
            total += c.process(f, t)
            t += 1 / 30
        for _ in range(10):                  # gone into the hub
            total += c.process(np.full((720, 1280, 3), 40, np.uint8), t)
            t += 1 / 30
    assert total == 6 and c.total == 6
    # a ball rolling past beside the outline is not counted
    for x in range(300, 1000, 12):
        f = np.full((720, 1280, 3), 40, np.uint8)
        cv2.circle(f, (x, 250), 15, (0, 220, 255), -1)
        total += c.process(f, t)
        t += 1 / 30
    assert total == 6


EXIT = {"line": [[480, 480], [800, 480]], "out": [640, 560]}     # across the chute below BOX


def test_exit_line_rides_along_with_the_outline_and_scales():
    hub = {"source": "0", "size": [1280, 720], "outline": BOX, "exit": EXIT, "ball_area": 400, "measured": 380}
    cfg = hubsetup.merge({**BASE, "line": [[1, 1], [2, 2]], "out": [3, 3]}, hub)
    assert cfg["line"] == EXIT["line"] and cfg["out"] == EXIT["out"] and "measured" not in cfg
    f = hubsetup.fit(cfg, 1920, 1080)
    assert f["line"] == [[720.0, 720.0], [1200.0, 720.0]] and f["out"] == [960.0, 840.0]
    assert f["ball_area"] == 900.0                                       # Measure's estimate never reaches it
    # no exit line (or an exit without an outline): the file's line is gone too
    cfg = hubsetup.merge({**BASE, "line": [[1, 1], [2, 2]], "out": [3, 3]}, {**hub, "exit": None})
    assert "line" not in cfg and "out" not in cfg
    cfg = hubsetup.merge(BASE, {**hub, "outline": []})
    assert "line" not in cfg and hubsetup.waiting(cfg) == "draw this hub's outline in Setup → Cameras"


def test_measure_estimate_is_the_median_of_lone_balls():
    import dropfit
    areas = [690, 700, 710, 705, 2000, 698, 702, 40, 699]               # a clump and a speck among singles
    assert dropfit.estimate(areas, 150) == {"area": 700.0, "blobs": 9, "frames": 150}
    assert dropfit.estimate(areas[:8], 150)["area"] == 701.0            # even count: between the middle two
    with pytest.raises(ValueError, match="Only 3 lone balls"):
        dropfit.estimate([700, 701, 702], 150)


def _fall(c, x, y0, y1, t, cv2, np):
    """One ball (707 px) falling from y0 to y1, then gone; returns (new count, t)."""
    n = 0
    for y in range(y0, y1, 9):
        f = np.full((720, 1280, 3), 40, np.uint8)
        cv2.circle(f, (x, y), 15, (0, 220, 255), -1)
        n += c.process(f, t)
        t += 1 / 30
    for _ in range(10):
        n += c.process(np.full((720, 1280, 3), 40, np.uint8), t)
        t += 1 / 30
    return n, t


def test_outline_and_exit_line_count_the_larger_never_the_sum():
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    from counters import load_counter
    cfg = hubsetup.fit(hubsetup.merge({"counter": "colour", "hub": "red"},
                                      {"source": "0", "size": [1280, 720], "outline": BOX, "exit": EXIT,
                                       "ball_area": 700}), 1280, 720)
    c = load_counter(cfg)
    t, total = 0.0, 0
    for b in range(6):                       # in through the opening and out through the exit: once each
        n, t = _fall(c, 560 + 30 * b, 120, 560, t, cv2, np)
        total += n
    assert total == 6 and c.total == 6
    for b in range(2):                       # seen only at the exit (missed at the opening): the exit leads
        n, t = _fall(c, 600 + 40 * b, 440, 560, t, cv2, np)
        total += n
    assert total == 8
    assert "exit 8" in c.status()["detail"]


def test_check_a_recording_recounts_it_with_the_hub_setup(tmp_path):
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    import run_vision
    rec = tmp_path / "test_20261103_101500_red.mp4"
    vw = cv2.VideoWriter(str(rec), cv2.VideoWriter_fourcc(*"mp4v"), 30, (1280, 720))
    for b in range(5):                       # 5 balls into the opening and out of the exit
        for y in list(range(120, 560, 9)) + [None] * 10:
            f = np.full((720, 1280, 3), 40, np.uint8)
            if y is not None:
                cv2.circle(f, (560 + 30 * b, y), 15, (0, 220, 255), -1)
            vw.write(f)
    vw.release()
    s = run_vision.Sender("http://127.0.0.1:1", "k")
    s.dirs = {"test": tmp_path}
    w = run_vision.HubWorker("red", {"counter": "colour"}, s, None, False)
    w.setup = {"source": "0", "size": [1280, 720], "outline": BOX, "exit": EXIT, "ball_area": 700}
    s.workers["red"] = w
    assert [f["name"] for f in s.files()] == [rec.name] and s.files()[0]["hub"] == "red"
    r = s._check({"id": 1, "dir": "test", "name": rec.name, "hub": "red", "hand": 5})
    assert [(x["label"], x["count"], x["error"]) for x in r["rows"]] == [
        ("what the hub counts now", 5, 0), ("outline alone", 5, 0), ("exit line alone", 5, 0)]
    s.scalers["red"].set(2)                  # counts per ball applies, as it does live
    assert s._check({"id": 2, "dir": "test", "name": rec.name, "hub": "red", "hand": 5})["rows"][0]["count"] == 2
    with pytest.raises(ValueError, match="isn't there"):
        s._check({"id": 3, "dir": "test", "name": "../event.yaml", "hub": "red", "hand": 5})
    w.setup = {**w.setup, "ball_area": None}
    with pytest.raises(ValueError, match="calibrate"):
        s._check({"id": 4, "dir": "test", "name": rec.name, "hub": "red", "hand": 5})
