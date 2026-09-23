"""Entry point: `python -m src`, which is what the Dockerfile runs."""

from __future__ import annotations

import asyncio

from .main import main

asyncio.run(main())
