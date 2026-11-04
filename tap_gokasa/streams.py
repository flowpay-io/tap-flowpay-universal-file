"""Stream type classes for tap-gokasa."""

from __future__ import annotations

import typing as t

from singer_sdk import Stream

from tap_gokasa.history import latest_history, parse_history
from tap_gokasa.schema import SCHEMA
from tap_gokasa.transactions import merge, parse_delivery

if t.TYPE_CHECKING:
    from tap_gokasa.tap import TapGoKasa


class NoWaitingFile(RuntimeError):
    """The job started but no file waits for the merchant."""


class TransactionsStream(Stream):
    """The merchant's complete transaction history, in GoKasa's own format.

    Every run emits the full history: the previous history file merged with every
    file waiting in inbound/. Downstream needs one complete file.
    """

    name = "transactions"
    primary_keys: t.ClassVar[list[str]] = ["transactionId"]
    schema = SCHEMA

    def __init__(self, tap: TapGoKasa) -> None:
        """Init the stream."""
        super().__init__(tap)
        self.tap = tap
        self.consumed_files: list[str] = []

    def get_records(self, context: dict | None) -> t.Iterable[dict]:  # noqa: ARG002
        """Merge the waiting files into the previous history and emit the result.

        Every file is read and checked before the first record goes out, so a bad
        file fails the job without emitting a partial history.
        """
        store = self.tap.store
        waiting = self.tap.waiting_files()
        if not waiting:
            msg = f"No file waits under {self.tap.inbound_prefix} in {self.tap.inbound_container}"
            raise NoWaitingFile(msg)
        deliveries = []
        for blob in waiting:
            records = parse_delivery(store.read(self.tap.inbound_container, blob.name), blob.name)
            self.logger.info("%s: %d transactions.", blob.name, len(records))
            deliveries.append(records)

        history_container = self.config["history_container"]
        previous = latest_history(store.list_blobs(history_container, self.tap.history_prefix))
        if previous is None:
            self.logger.info("No previous history under %s, so this is the initial file.", self.tap.history_prefix)
            history: list[dict] = []
        else:
            history = parse_history(store.read(history_container, previous.name), previous.name)
            self.logger.info("Previous history %s (%s): %d transactions.",
                             previous.name, previous.last_modified, len(history))

        records, changed = merge(history, *deliveries)
        if changed:
            self.logger.warning(
                "%d transactions changed since an earlier file; kept the newest version. First ids: %s",
                len(changed),
                changed[:20],
            )
        self.logger.info("Emitting %d transactions (%d new).", len(records), len(records) - len(history))
        self.consumed_files = [blob.name for blob in waiting]
        yield from records
