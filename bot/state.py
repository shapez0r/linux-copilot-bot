"""Persistent owner settings: the active repository and model."""
from __future__ import annotations

import json
import os
from pathlib import Path


class State:
    def __init__(self, path: Path, default_model: str):
        self._path = path
        self.repo = ""  # "owner/name" of the active repository.
        self.model = default_model
        try:
            data = json.loads(path.read_text())
            self.repo = str(data.get("repo", ""))
            self.model = str(data.get("model", default_model))
        except (OSError, ValueError, AttributeError):
            pass

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"repo": self.repo, "model": self.model}))
        os.replace(tmp, self._path)
