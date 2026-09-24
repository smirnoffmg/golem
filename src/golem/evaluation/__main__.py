import os
import sys

from golem.evaluation.cli import main

raise SystemExit(main(sys.argv[1:], os.environ))
