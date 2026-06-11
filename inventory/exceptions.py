class InventoryError(Exception):
    def __init__(self, error_code: str, message: str, data: dict | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.data = data or {}
