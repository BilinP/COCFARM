"""farmbot: a clean-room Clash of Clans farming bot driven over ADB."""

import os

# numpy's OpenBLAS sets aside a buffer for every CPU thread when it loads
# (half a gigabyte on a 16-thread PC); the bot does no big matrix sums, so
# one thread is plenty. Set before anything imports numpy.
for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ.setdefault(_name, "1")

__version__ = "1.5.2"
