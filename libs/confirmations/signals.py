from __future__ import annotations

import uuid


class ConfirmationRequiredError(Exception):
    def __init__(self, confirmation_id: uuid.UUID, summary: str) -> None:
        super().__init__(summary)
        self.confirmation_id = confirmation_id
        self.summary = summary
