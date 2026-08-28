"""Git-backed mailbox: outbox, inbox, readbox."""

from darkmatter.gitbox.mailbox import Mailbox, get_mailbox, reset_mailbox

__all__ = ["Mailbox", "get_mailbox", "reset_mailbox"]
