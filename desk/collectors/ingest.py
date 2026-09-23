"""Write collector output: raw records (deduped on content_hash), series observations,
completed price bars and latest quotes. Set-based statements use unnest because
RETURNING does not survive an executemany of text()."""

from dataclasses import dataclass

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from desk.artifacts.store import append_artifact
from desk.collectors.base import (
    AccountSnapshot,
    CollectResult,
    GridObservation,
    PriceBar,
    QuoteSnapshot,
    RecordEmbedding,
)

CONTENT_HASH_INDEX = "raw_records_content_hash_uq"


@dataclass(frozen=True, slots=True)
class IngestCounts:
    records_added: int
    records_duplicate: int
    observations_added: int
    bars_added: int = 0
    quotes_updated: int = 0
    snapshots_added: int = 0
    embeddings_added: int = 0
    grid_added: int = 0

    @property
    def stored(self) -> int:
        """Rows that are new data (quote refreshes overwrite, so they are not counted)."""
        return (
            self.records_added
            + self.observations_added
            + self.bars_added
            + self.snapshots_added
            + self.embeddings_added
            + self.grid_added
        )


def _existing_hashes(conn: Connection, hashes: list[str]) -> set[str]:
    if not hashes:
        return set()
    rows = conn.execute(
        text(
            "SELECT payload->>'content_hash' FROM artifacts "
            "WHERE kind = 'raw_record' AND payload->>'content_hash' = ANY(:hashes)"
        ),
        {"hashes": hashes},
    )
    return {row[0] for row in rows}


def _is_content_hash_conflict(exc: IntegrityError) -> bool:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == CONTENT_HASH_INDEX


def ingest(conn: Connection, result: CollectResult) -> IngestCounts:
    """Store new records and observations. The caller owns the transaction."""
    seen = _existing_hashes(conn, [record.content_hash for record in result.records])
    added = 0
    duplicate = 0
    for record in result.records:
        if record.content_hash in seen:
            duplicate += 1
            continue
        seen.add(record.content_hash)
        # Another collector can insert the same content between the lookup and this
        # insert (the EDGAR feed and per-company collectors share filings). The savepoint
        # turns that race into a duplicate count instead of failing the whole batch.
        try:
            with conn.begin_nested():
                append_artifact(conn, record)
        except IntegrityError as exc:
            if not _is_content_hash_conflict(exc):
                raise
            duplicate += 1
            continue
        added += 1

    observations_added = 0
    if result.observations:
        inserted = conn.execute(
            text(
                "INSERT INTO series_observations "
                "(source, series_id, period, value, unit, fetched_at) "
                "SELECT * FROM unnest("
                "CAST(:sources AS text[]), CAST(:series_ids AS text[]), "
                "CAST(:periods AS date[]), CAST(:values AS numeric[]), "
                "CAST(:units AS text[]), CAST(:fetched AS timestamptz[])) "
                "ON CONFLICT (source, series_id, period, value) DO NOTHING "
                "RETURNING id"
            ),
            {
                "sources": [o.source for o in result.observations],
                "series_ids": [o.series_id for o in result.observations],
                "periods": [o.period for o in result.observations],
                "values": [o.value for o in result.observations],
                "units": [o.unit for o in result.observations],
                "fetched": [o.fetched_at for o in result.observations],
            },
        )
        observations_added = len(inserted.all())

    return IngestCounts(
        records_added=added,
        records_duplicate=duplicate,
        observations_added=observations_added,
        bars_added=_insert_bars(conn, result.bars),
        quotes_updated=_upsert_quotes(conn, result.quotes),
        snapshots_added=sum(_insert_snapshot(conn, snapshot) for snapshot in result.accounts),
        embeddings_added=_insert_embeddings(conn, result.embeddings),
        grid_added=_insert_grid(conn, result.grid),
    )


def _insert_embeddings(conn: Connection, embeddings: list[RecordEmbedding]) -> int:
    if not embeddings:
        return 0
    inserted = conn.execute(
        text(
            "INSERT INTO raw_record_embeddings (artifact_id, model, embedding) "
            "SELECT i, m, CAST(v AS vector) FROM unnest("
            "CAST(:ids AS uuid[]), CAST(:models AS text[]), CAST(:vectors AS text[])) "
            "AS t(i, m, v) "
            # A record purged between selection and insert has no row to reference.
            "WHERE EXISTS (SELECT 1 FROM artifacts a WHERE a.id = t.i) "
            "ON CONFLICT (artifact_id, model) DO NOTHING RETURNING artifact_id"
        ),
        {
            "ids": [e.artifact_id for e in embeddings],
            "models": [e.model for e in embeddings],
            "vectors": ["[" + ",".join(repr(x) for x in e.vector) + "]" for e in embeddings],
        },
    )
    return len(inserted.all())


def _insert_grid(conn: Connection, observations: list[GridObservation]) -> int:
    if not observations:
        return 0
    inserted = conn.execute(
        text(
            "INSERT INTO grid_observations "
            "(iso, series_id, interval_start, interval_minutes, value, unit, fetched_at) "
            "SELECT * FROM unnest("
            "CAST(:isos AS text[]), CAST(:series AS text[]), CAST(:starts AS timestamptz[]), "
            "CAST(:minutes AS integer[]), CAST(:values AS numeric[]), CAST(:units AS text[]), "
            "CAST(:fetched AS timestamptz[])) "
            "ON CONFLICT (iso, series_id, interval_start) DO NOTHING RETURNING iso"
        ),
        {
            "isos": [o.iso for o in observations],
            "series": [o.series_id for o in observations],
            "starts": [o.interval_start for o in observations],
            "minutes": [o.interval_minutes for o in observations],
            "values": [o.value for o in observations],
            "units": [o.unit for o in observations],
            "fetched": [o.fetched_at for o in observations],
        },
    )
    return len(inserted.all())


def _insert_snapshot(conn: Connection, snapshot: AccountSnapshot) -> int:
    """Store one account snapshot with its positions; 0 if that as_of is already stored."""
    snapshot_id = conn.execute(
        text(
            "INSERT INTO account_snapshots (source, broker, account_ref, as_of, fetched_at, "
            "net_liquidation, cash, settled_cash, buying_power, currency) "
            "VALUES (:source, :broker, :account_ref, :as_of, :fetched_at, :net_liquidation, "
            ":cash, :settled_cash, :buying_power, :currency) "
            "ON CONFLICT (source, account_ref, as_of) DO NOTHING RETURNING id"
        ),
        {
            "source": snapshot.source,
            "broker": snapshot.broker,
            "account_ref": snapshot.account_ref,
            "as_of": snapshot.as_of,
            "fetched_at": snapshot.fetched_at,
            "net_liquidation": snapshot.net_liquidation,
            "cash": snapshot.cash,
            "settled_cash": snapshot.settled_cash,
            "buying_power": snapshot.buying_power,
            "currency": snapshot.currency,
        },
    ).scalar_one_or_none()
    if snapshot_id is None:
        return 0
    if snapshot.positions:
        conn.execute(
            text(
                "INSERT INTO position_snapshots (snapshot_id, symbol, contract, asset_class, "
                "quantity, multiplier, avg_cost, cost_basis, mark_price, market_value, currency) "
                "VALUES (:snapshot_id, :symbol, :contract, :asset_class, :quantity, :multiplier, "
                ":avg_cost, :cost_basis, :mark_price, :market_value, :currency)"
            ),
            [
                {
                    "snapshot_id": snapshot_id,
                    "symbol": p.symbol,
                    "contract": p.contract,
                    "asset_class": p.asset_class,
                    "quantity": p.quantity,
                    "multiplier": p.multiplier,
                    "avg_cost": p.avg_cost,
                    "cost_basis": p.cost_basis,
                    "mark_price": p.mark_price,
                    "market_value": p.market_value,
                    "currency": p.currency,
                }
                for p in snapshot.positions
            ],
        )
    return 1


def _insert_bars(conn: Connection, bars: list[PriceBar]) -> int:
    if not bars:
        return 0
    inserted = conn.execute(
        text(
            "INSERT INTO price_bars "
            "(source, symbol, interval, ts, open, high, low, close, volume, fetched_at) "
            "SELECT * FROM unnest("
            "CAST(:sources AS text[]), CAST(:symbols AS text[]), CAST(:intervals AS text[]), "
            "CAST(:ts AS timestamptz[]), CAST(:opens AS numeric[]), CAST(:highs AS numeric[]), "
            "CAST(:lows AS numeric[]), CAST(:closes AS numeric[]), "
            "CAST(:volumes AS numeric[]), CAST(:fetched AS timestamptz[])) "
            "ON CONFLICT (source, symbol, interval, ts) DO NOTHING "
            "RETURNING ts"
        ),
        {
            "sources": [b.source for b in bars],
            "symbols": [b.symbol for b in bars],
            "intervals": [b.interval for b in bars],
            "ts": [b.ts for b in bars],
            "opens": [b.open for b in bars],
            "highs": [b.high for b in bars],
            "lows": [b.low for b in bars],
            "closes": [b.close for b in bars],
            "volumes": [b.volume for b in bars],
            "fetched": [b.fetched_at for b in bars],
        },
    )
    return len(inserted.all())


def _upsert_quotes(conn: Connection, quotes: list[QuoteSnapshot]) -> int:
    if not quotes:
        return 0
    updated = conn.execute(
        text(
            "INSERT INTO quotes_latest "
            "(symbol, source, bid, ask, bid_size, ask_size, quote_time, updated_at) "
            "SELECT s, src, b, a, bs, az, qt, now() FROM unnest("
            "CAST(:symbols AS text[]), CAST(:sources AS text[]), CAST(:bids AS numeric[]), "
            "CAST(:asks AS numeric[]), CAST(:bid_sizes AS numeric[]), "
            "CAST(:ask_sizes AS numeric[]), CAST(:times AS timestamptz[])) "
            "AS q(s, src, b, a, bs, az, qt) "
            "ON CONFLICT (symbol) DO UPDATE SET source = EXCLUDED.source, bid = EXCLUDED.bid, "
            "ask = EXCLUDED.ask, bid_size = EXCLUDED.bid_size, ask_size = EXCLUDED.ask_size, "
            "quote_time = EXCLUDED.quote_time, updated_at = now() "
            # A reconnect can replay an older quote; never move a symbol backwards in time.
            "WHERE quotes_latest.quote_time <= EXCLUDED.quote_time "
            "RETURNING symbol"
        ),
        {
            "symbols": [q.symbol for q in quotes],
            "sources": [q.source for q in quotes],
            "bids": [q.bid for q in quotes],
            "asks": [q.ask for q in quotes],
            "bid_sizes": [q.bid_size for q in quotes],
            "ask_sizes": [q.ask_size for q in quotes],
            "times": [q.quote_time for q in quotes],
        },
    )
    return len(updated.all())
