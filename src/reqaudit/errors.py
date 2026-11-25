"""Errors that the pipeline turns into a report instead of a crash."""


class InputError(Exception):
    """The input document cannot be accepted.

    `code` is a stable machine-readable reason, `message` is user-facing Russian text
    that goes into the report and the console.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
