class BookingError(Exception):
    """A refusal the page shows: HTTP status, message, and optional extra JSON
    (e.g. `needs` = the passenger form, `field_errors`)."""

    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra
