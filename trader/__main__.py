"""Allow `python -m trader <subcommand>`."""
from .cli import main
import sys

sys.exit(main())
