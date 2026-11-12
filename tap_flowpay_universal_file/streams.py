"""Stream type classes for tap-gokasa."""

from __future__ import annotations

import typing as t

from singer_sdk import Stream

if t.TYPE_CHECKING:
    from tap_flowpay_universal_file.tap import TapFlowpayUniversalFile


class NoWaitingFile(RuntimeError):
    """The job started but no file waits for the merchant."""


class FileHistoryStream(Stream):
    """The merchant's complete history in the format of the configured file contract.

    Every run emits the full history: the previous history file merged with every
    selected delivery. Downstream needs one complete file.
    """

    def __init__(self, tap: TapFlowpayUniversalFile) -> None:
        """Init the stream."""
        super().__init__(tap, schema=tap.contract.schema, name=tap.contract.stream_name)
        self.primary_keys = list(tap.contract.key_properties)
        self.tap = tap

    def get_records(self, context: dict | None) -> t.Iterable[dict]:  # noqa: ARG002
        """Merge the waiting files into the previous history and emit the result.

        Every file is read and checked before the first record goes out, so a bad
        file fails the job without emitting a partial history.
        """
        store = self.tap.store
        contract = self.tap.contract
        waiting = self.tap.input_files()
        if not waiting:
            msg = f"No file waits under {self.tap.inbound_prefix} in {self.tap.inbound_container}"
            raise NoWaitingFile(msg)
        deliveries = []
        for name in waiting:
            delivery = contract.read_delivery(store.read(self.tap.inbound_container, name), name)
            self.logger.info("%s: %d records.", name, len(delivery.records))
            if delivery.ignored:
                fields = ", ".join(f"{path} ({count} records)" for path, count in delivery.ignored.items())
                self.logger.warning("%s: ignored fields that are not configured: %s", name, fields)
            deliveries.append(delivery.records)

        history_container = self.config["history_container"]
        previous = self.tap.previous_history()
        if previous is None:
            self.logger.info("No previous history selected; this is the initial file.")
            history: list[dict] = []
        else:
            history = contract.read_history(store.read(history_container, previous), previous)
            self.logger.info("Previous history %s: %d records.", previous, len(history))

        records, changed = contract.merge(history, *deliveries)
        if changed:
            self.logger.warning(
                "%d records changed since an earlier file; kept the delivery's version. First ids: %s",
                len(changed),
                changed[:20],
            )
        self.logger.info("Emitting %d records (%d new).", len(records), len(records) - len(history))
        yield from records
