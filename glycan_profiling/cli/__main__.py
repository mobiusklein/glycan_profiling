import os
import sys
import traceback

from multiprocessing import freeze_support

from glycan_profiling.cli import (
    base, build_db, tools, mzml, analyze, config,
    export)

try:
    from glycresoft_app.cli import server
except ImportError as e:
    pass


def info(type, value, tb):
    if hasattr(sys, 'ps1') or not sys.stderr.isatty():
        sys.__excepthook__(type, value, tb)
    else:
        import ipdb
        traceback.print_exception(type, value, tb)
        ipdb.post_mortem(tb)


sys.excepthook = info


def main():
    freeze_support()
    if os.getenv("GLYCRESOFTDEBUG"):
        sys.excepthook = info
    base.cli.main(standalone_mode=True)


if __name__ == '__main__':
    main()
