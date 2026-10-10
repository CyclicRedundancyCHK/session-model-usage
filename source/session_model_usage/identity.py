"""Resolve local client identities through Codex's explicit persisted bindings."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from urllib.parse import unquote


UUID = r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}'
CLIENT = re.compile(r'client-new-thread:' + UUID)
THREAD_ID = re.compile(UUID)
LOCAL_KEY = re.compile(r'thread-client-id-v1:local:(' + UUID + r')')


class ClientBindings:
    # This file also contains unrelated state. Retain only validated ID pairs;
    # never expose its other fields, or scan authentication/session files.
    LIMIT = 2 * 1024 * 1024

    def __init__(self, path: Path | None = None):
        home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        self.path = path if path is not None else home / '.codex-global-state.json'
        self.signature = None
        self.bindings = {}

    def resolve(self, client: str | None) -> str | None:
        if not isinstance(client, str) or not CLIENT.fullmatch(client):
            return None
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if stat.st_size > self.LIMIT:
                raise ValueError('state exceeds read bound')
            if signature != self.signature:
                with self.path.open('rb') as stream:
                    raw = stream.read(self.LIMIT + 1)
                if len(raw) > self.LIMIT:
                    raise ValueError('state exceeds read bound')
                document = json.loads(raw)
                atoms = document.get('electron-persisted-atom-state') if isinstance(document, dict) else None
                if not isinstance(atoms, dict):
                    raise ValueError('binding state unavailable')
                candidates = {}

                def add(candidate, thread):
                    if (isinstance(candidate, str) and CLIENT.fullmatch(candidate) and
                            isinstance(thread, str) and THREAD_ID.fullmatch(thread)):
                        candidates.setdefault(candidate.lower(), set()).add(thread.lower())

                direct = atoms.get('client-thread-bindings-v1')
                if isinstance(direct, dict):
                    for candidate, thread in direct.items():
                        add(candidate, thread)
                for key, candidate in atoms.items():
                    if not key.startswith('thread-client-id-v1:'):
                        continue
                    match = LOCAL_KEY.fullmatch(unquote(key))
                    if match:
                        add(candidate, match[1])
                self.bindings = {candidate: next(iter(threads)) for candidate, threads in candidates.items()
                                 if len(threads) == 1}
                self.signature = signature
            return self.bindings.get(client.lower())
        except (OSError, ValueError, TypeError):
            # A partial write, deletion or unsupported state must clear a
            # previously resolved identity rather than show stale usage.
            self.signature = None
            self.bindings.clear()
            return None
