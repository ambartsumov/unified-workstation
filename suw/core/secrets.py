"""Secret references. Configuration holds names (`keychain://name`); values live in the
operating system's credential store (Keychain, Secret Service, Windows Credential Manager —
see `suw.platform`). Values are never written to config, logs or the inventory.
"""

from __future__ import annotations

from .. import platform as osplatform
from .. import product

SERVICE = product.CREDENTIAL_SERVICE


class SecretError(RuntimeError):
    pass


def _name(ref: str) -> str:
    return ref.split("://", 1)[1] if "://" in ref else ref


def get(ref: str) -> str | None:
    return osplatform.current().credential_get(SERVICE, _name(ref))


def put(ref: str, value: str) -> None:
    if not osplatform.current().credential_put(SERVICE, _name(ref), value):
        raise SecretError("the system credential store refused the secret (is the keyring unlocked and installed?)")


def delete(ref: str) -> bool:
    return osplatform.current().credential_delete(SERVICE, _name(ref))


def backend() -> str:
    return osplatform.current().credential_backend()
