"""Expected failures, with messages safe to show to users and to models.

Every message says what went wrong and, where possible, what to do next.
Messages never include the pairing key or raw library exceptions.
"""


class LgtvError(Exception):
    """Base class for anticipated failures."""


class ConfigError(LgtvError):
    """The config file is missing, unreadable or invalid."""


class NotConfigured(LgtvError):
    """No TV has been paired yet."""

    def __init__(self) -> None:
        super().__init__("No TV paired yet. Run discovery, then pair a TV.")


class UnknownTv(LgtvError):
    """A TV name that is not in the config."""

    def __init__(self, name: str, known: list[str]) -> None:
        options = ", ".join(sorted(known)) or "none"
        super().__init__(f"No TV named '{name}'. Known TVs: {options}.")


class Unreachable(LgtvError):
    """The TV did not answer on the network."""


class ConnectionLost(Unreachable):
    """An established connection dropped; callers may reconnect and retry once."""


class TvMoved(Unreachable):
    """A device claiming to be the TV answered at a new address.

    The claim cannot be verified (the TV's id is broadcast in the clear), so
    the pairing key is not sent there until a person confirms the address.
    """

    def __init__(self, name: str, old_host: str, new_host: str) -> None:
        self.new_host = new_host
        super().__init__(
            f"TV '{name}' is not at {old_host}, but a device claiming to be it answered at "
            f"{new_host}. If that is your TV, confirm the new address (CLI: "
            f"lgtv move {name} {new_host}; MCP: set_tv_address). A DHCP reservation on "
            "your router keeps the address fixed."
        )


class Rejected(LgtvError):
    """The TV understood the request and refused it."""

    def __init__(self) -> None:
        super().__init__("The TV rejected the request.")


class TvOff(LgtvError):
    """The TV is connected but in standby."""

    def __init__(self, name: str) -> None:
        super().__init__(f"TV '{name}' is off. Turn it on first.")


class PairingFailed(LgtvError):
    """The TV refused or never confirmed pairing."""


class PermissionDenied(LgtvError):
    """The TV refused an action for lack of permission."""

    def __init__(self) -> None:
        super().__init__("The TV denied permission for this action.")


class NotFound(LgtvError):
    """No app, input or key matched the request."""


class Ambiguous(LgtvError):
    """Several apps or inputs matched the request."""


class InvalidInput(LgtvError):
    """An argument failed validation before reaching the TV."""
