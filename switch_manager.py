#!/usr/bin/env python3
"""Entry point: python switch_manager.py --help"""

import sys

from switch_manager.cli import main

if __name__ == "__main__":
    sys.exit(main())
