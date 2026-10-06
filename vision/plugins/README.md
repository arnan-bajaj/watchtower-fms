# Counter plugins

Two ways to add a counter, neither needs any code or config edited:

1. **One file:** copy `_example.py` to `my_counter.py` here, change its
   `NAME`, and set `counter: <that NAME>` in `config/vision.yaml`.
2. **A whole repo** (e.g. TBACroppedOutVid's colour/combo counters):

   ```bash
   python run_vision.py --add-plugin ~/dev/TBACroppedOutVid
   ```

   This writes `TBACroppedOutVid.path` here (one line: the folder) and prints
   the counter names it found. `--remove-plugin TBACroppedOutVid` undoes it.
   Copying or cloning a repo straight into this folder works too.

`python run_vision.py --list-counters` shows every counter and where it came
from. Built-in names (`zone`, `linecross`, `mock`) cannot be taken over.
Everything here except this README and `_example.py` is ignored by git, so
your own plugins stay local.
