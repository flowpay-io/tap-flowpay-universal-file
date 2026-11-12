"""Stream type classes for tap-gokasa."""

from __future__ import annotations

import typing as t

from singer_sdk import Stream

if t.TYPE_CHECKING:
    from tap_gokasa.tap import TapGoKasa


class NoWaitingFile(RuntimeError):
    """The job started but no file waits for the merchant."""


class FileHistoryStream(Stream):
    """The merchant's complete history in the selected adapter's format.

    Every run emits the full history: the previous history file merged with every
    selected delivery. Downstream needs one complete file.
    """

    def __init__(self, tap: TapGoKasa) -> None:
        """Init the stream."""
        super().__init__(tap, schema=tap.adapter.schema, name=tap.adapter.stream_name)
        self.primary_keys = list(tap.adapter.primary_keys)
        self.tap = tap

    def get_records(self, context: dict | None) -> t.Iterable[dict]:  # noqa: ARG002
        """Merge the waiting files into the previous history and emit the result.

        Every file is read and checked before the first record goes out, so a bad
        file fails the job without emitting a partial history.
        """
        store = self.tap.store
        adapter = self.tap.adapter
        waiting = self.tap.input_files()
        if not waiting:
            msg = f"No file waits under {self.tap.inbound_prefix} in {self.tap.inbound_container}"
            raise NoWaitingFile(msg)
        deliveries = []
        for name in waiting:
            records = adapter.parse_delivery(store.read(self.tap.inbound_container, name), name)
            self.logger.info("%s: %d records.", name, len(records))
            deliveries.append(records)

        history_container = self.config["history_container"]
        previous = self.tap.previous_history()
        if previous is None:
            self.logger.info("No previous history selected; this is the initial file.")
            history: list[dict] = []
        else:
            history = adapter.parse_history(store.read(history_container, previous), previous)
            self.logger.info("Previous history %s: %d records.", previous, len(history))

        records, changed = adapter.merge(history, *deliveries)
        if changed:
            self.logger.warning(
                "%d records changed since an earlier file; kept the delivery's version. First ids: %s",
                len(changed),
                changed[:20],
            )
        self.logger.info("Emitting %d records (%d new).", len(records), len(records) - len(history))
        yield from records
